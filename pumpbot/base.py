"""Shared machinery for all strategies: orders, exits, risk checks, state, run loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any

from .config import Config
from .models import TradeEvent
from .positions import Position, PositionManager

log = logging.getLogger("pumpbot")


class BaseBot:
    maintenance_interval_sec: float = 60.0
    tick_interval_sec: float = 1.0

    def __init__(self, cfg: Config, executor, feed=None, clock=time.time, positions_cfg: Config | None = None):
        self.cfg = cfg
        self.log = logging.getLogger("pumpbot")
        self.executor = executor
        self.feed = feed
        self.clock = clock
        self.positions = PositionManager(positions_cfg or cfg)
        self.prices: dict[str, float] = {}
        self.reserves: dict[str, float] = {}  # SOL in the bonding curve, for price impact
        self._seen_sigs: OrderedDict[str, None] = OrderedDict()
        self._busy: set[str] = set()  # mints with an order in flight
        self._buying: set[str] = set()
        self._exit_retry: dict[str, str] = {}  # mint -> reason, for failed sells
        self._tasks: set[asyncio.Task] = set()
        # async callbacks: enter_hooks(bot, mint, price, probability, label), exit_hooks(bot, mint, reason)
        self.enter_hooks: list = []
        self.exit_hooks: list = []
        if hasattr(executor, "price_fn"):  # paper fills use the price *after* latency
            executor.price_fn = self.prices.get
            executor.reserve_fn = self.reserves.get

    # ---------------------------------------------------------------- state
    def state_extra(self) -> dict[str, Any]:
        return {}

    def load_extra(self, data: dict[str, Any]) -> None:
        pass

    def load_state(self) -> None:
        if not os.path.exists(self.cfg.state_file):
            return
        with open(self.cfg.state_file) as f:
            data = json.load(f)
        self.positions.load(data.get("positions", {}))
        if self.cfg.dry_run and "paper_balance" in data and hasattr(self.executor, "balance_sol"):
            self.executor.balance_sol = data["paper_balance"]
        self.load_extra(data)
        self.log.info("restored state: %d open positions", len(self.positions.open))

    def save_state(self) -> None:
        data: dict[str, Any] = {}
        if os.path.exists(self.cfg.state_file):  # keep other strategies' data
            try:
                with open(self.cfg.state_file) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
        data["positions"] = self.positions.to_dict()
        data.update(self.state_extra())
        if hasattr(self.executor, "balance_sol"):
            data["paper_balance"] = self.executor.balance_sol
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.cfg.state_file)

    # --------------------------------------------------------------- events
    def _dup(self, sig: str) -> bool:
        if not sig:
            return False
        if sig in self._seen_sigs:
            return True
        self._seen_sigs[sig] = None
        if len(self._seen_sigs) > 20000:
            self._seen_sigs.popitem(last=False)
        return False

    def parse(self, msg: dict) -> TradeEvent | None:
        evt = TradeEvent.from_pumpportal(msg, now=self.clock())
        if evt is None or self._dup(evt.signature):
            return None
        self.prices[evt.mint] = evt.price
        try:
            v_sol = float(msg.get("vSolInBondingCurve") or 0)
            if v_sol > 0:
                self.reserves[evt.mint] = v_sol
        except (TypeError, ValueError):
            pass
        return evt

    async def _update_position(self, evt: TradeEvent) -> None:
        pos = self.positions.open.get(evt.mint)
        if pos is None:
            return
        was_runner = pos.runner
        reason = self.positions.exit_reason(evt.mint, evt.price, evt.timestamp)
        if reason:
            await self._exit(evt.mint, reason)
        elif pos.runner and not was_runner:
            c = self.positions.cfg
            self.log.info(
                "RUNNER %s… hit +%.0f%%: letting it run (trail %.0f%% from peak, floor +%.0f%%)",
                evt.mint[:8], c.take_profit_pct, c.trailing_stop_pct, c.tp_floor_pct,
            )

    # --------------------------------------------------------------- orders
    def _spawn(self, coro) -> None:
        """Orders run as tasks so the feed keeps flowing while we wait for confirmation."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _enter(
        self, mint: str, price_hint: float, now: float, leaders: list[str] | None = None,
        probability: float = 0.0, label: str = "",
    ) -> bool:
        if mint in self._busy:
            return False
        ok, why = self.positions.can_open(mint, now, await self.executor.balance(), pending=len(self._buying))
        if not ok:
            self.log.debug("  not entering %s…: %s", mint[:8], why)
            return False
        self._busy.add(mint)
        self._buying.add(mint)
        self._spawn(self._do_buy(mint, price_hint, leaders or [], probability, label))
        for hook in self.enter_hooks:
            await hook(self, mint, price_hint, probability, label)
        return True

    async def _do_buy(self, mint: str, price_hint: float, leaders: list[str], probability: float, label: str) -> None:
        c = self.positions.cfg
        try:
            fill = await self.executor.buy(mint, c.buy_amount_sol, self.prices.get(mint, price_hint))
            if not fill.ok:
                self.log.warning("  buy %s… failed: %s", mint[:8], fill.error)
                self.positions.last_trade_at[mint] = self.clock()  # don't hammer a failing mint
                return
            self.positions.add(Position(mint, fill.price, fill.sol, fill.tokens, self.clock(), leaders, probability))
            self.on_bought(mint)
            self.log.info(
                "BUY %s %s%.4f SOL -> %.0f tokens @ %.3e (TP %.3e / SL %.3e) tx=%s",
                mint, f"[{label}] " if label else "", fill.sol, fill.tokens, fill.price,
                fill.price * (1 + c.take_profit_pct / 100), fill.price * (1 - c.stop_loss_pct / 100),
                fill.signature,
            )
        except Exception:
            self.log.exception("buy %s… crashed", mint[:8])
        finally:
            self._busy.discard(mint)
            self._buying.discard(mint)

    def on_bought(self, mint: str) -> None:
        pass

    async def _exit(self, mint: str, reason: str) -> None:
        if mint not in self.positions.open or mint in self._busy:
            return
        self._busy.add(mint)
        self._spawn(self._do_exit(mint, reason))
        for hook in self.exit_hooks:
            await hook(self, mint, reason)

    async def _do_exit(self, mint: str, reason: str) -> None:
        try:
            pos = self.positions.open.get(mint)
            if pos is None:
                return
            fill = await self.executor.sell(mint, pos.tokens, self.prices.get(mint, pos.last_price))
            if not fill.ok:
                self.log.warning("sell %s… failed: %s (will retry)", mint[:8], fill.error)
                self._exit_retry[mint] = reason
                return
            self._exit_retry.pop(mint, None)
            trade = self.positions.close(mint, fill.sol, self.clock(), reason)
            self.log.info(
                "SELL %s… %s pnl=%+.4f SOL (%+.1f%%) held %.0fs tx=%s",
                mint[:8], reason, trade.pnl_sol, trade.pnl_sol / trade.sol_in * 100,
                trade.closed_at - trade.opened_at, fill.signature,
            )
        except Exception:
            self.log.exception("sell %s… crashed (will retry)", mint[:8])
            self._exit_retry[mint] = reason
        finally:
            self._busy.discard(mint)

    # ---------------------------------------------------------- maintenance
    async def tick(self) -> None:
        now = self.clock()
        for mint, reason in list(self._exit_retry.items()):
            await self._exit(mint, reason)
        for mint in list(self.positions.open):
            reason = self.positions.exit_reason(mint, 0.0, now)
            if reason:
                await self._exit(mint, reason)

    def status_extra(self) -> str:
        return ""

    async def maintenance(self) -> None:
        self.save_state()
        st = self.positions.stats()
        self.log.info(
            "status: balance=%.3f SOL open=%d closed=%d win=%.0f%% pnl=%+.4f SOL %s",
            await self.executor.balance(), st["open"], st["closed"], st["win_rate"] * 100,
            st["pnl_sol"], self.status_extra(),
        )

    async def on_start(self) -> None:
        pass

    async def handle(self, msg: dict) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def run(self) -> None:
        from .feed import PumpPortalFeed

        self.load_state()
        if not self.cfg.api_key:
            self.log.warning(
                "PUMPPORTAL_API_KEY not set: PumpPortal only streams new-token events without a "
                "funded API key, so trades cannot be watched. See README."
            )
        if self.feed is None:
            self.feed = PumpPortalFeed(self.cfg.ws_endpoint, self.handle)
        await self.feed.subscribe_tokens(self.positions.open)
        await self.on_start()
        feed_task = asyncio.create_task(self.feed.run())
        last_maint = self.clock()
        try:
            while not feed_task.done():
                await asyncio.sleep(self.tick_interval_sec)
                await self.tick()
                if self.clock() - last_maint >= self.maintenance_interval_sec:
                    await self.maintenance()
                    last_maint = self.clock()
        finally:
            self.feed.stop()
            feed_task.cancel()
            await self.drain()
            self.save_state()
            await self.executor.close()
