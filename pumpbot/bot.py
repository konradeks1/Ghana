"""Orchestrates discovery -> analysis -> entry -> +TP exit."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict

from .analyzer import EntryAnalyzer
from .config import Config
from .leaderboard import Leaderboard
from .models import TradeEvent
from .outcomes import EntryOutcomeTracker
from .positions import Position, PositionManager

log = logging.getLogger("pumpbot")


class CopyTradeBot:
    def __init__(self, cfg: Config, executor, feed=None, clock=time.time):
        self.cfg = cfg
        self.executor = executor
        self.feed = feed
        self.clock = clock
        self.leaderboard = Leaderboard(
            cfg.leaderboard_window_hours, cfg.min_round_trips, cfg.exclude_wallets, cfg.min_median_hold_sec
        )
        self.outcomes = EntryOutcomeTracker(cfg.take_profit_pct, cfg.stop_loss_pct, cfg.outcome_horizon_sec)
        self.analyzer = EntryAnalyzer(cfg, self.leaderboard)
        self.positions = PositionManager(cfg)
        self.prices: dict[str, float] = {}
        self.followed: set[str] = set()
        self.seed_traders: list[str] = self._load_seeds()
        self.tracked_tokens: OrderedDict[str, float] = OrderedDict()  # discovery set
        self._seen_sigs: OrderedDict[str, None] = OrderedDict()
        self._busy: set[str] = set()  # mints with an order in flight
        self._buying: set[str] = set()
        self._exit_retry: dict[str, str] = {}  # mint -> reason, for failed sells
        self._tasks: set[asyncio.Task] = set()
        if hasattr(executor, "price_fn"):  # paper fills use the price *after* latency
            executor.price_fn = self.prices.get

    # ------------------------------------------------------------------ setup
    def _load_seeds(self) -> list[str]:
        path = self.cfg.seed_traders_file
        if not path or not os.path.exists(path):
            return []
        with open(path) as f:
            return [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]

    def load_state(self) -> None:
        if not os.path.exists(self.cfg.state_file):
            return
        with open(self.cfg.state_file) as f:
            data = json.load(f)
        self.leaderboard.load(data.get("leaderboard", {}))
        self.positions.load(data.get("positions", {}))
        if self.cfg.dry_run and "paper_balance" in data and hasattr(self.executor, "balance_sol"):
            self.executor.balance_sol = data["paper_balance"]
        log.info("restored %d wallets, %d open positions", len(self.leaderboard.wallets), len(self.positions.open))

    def save_state(self) -> None:
        data = {
            "leaderboard": self.leaderboard.to_dict(),
            "positions": self.positions.to_dict(),
            "followed": sorted(self.followed),
        }
        if hasattr(self.executor, "balance_sol"):
            data["paper_balance"] = self.executor.balance_sol
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.cfg.state_file)

    # ------------------------------------------------------------ followed set
    async def refresh_followed(self) -> None:
        now = self.clock()
        top = self.leaderboard.top(self.cfg.top_n_traders, now)
        followed = [s.wallet for s in top]
        for w in self.seed_traders:  # fill remaining slots while the board warms up
            if len(followed) >= self.cfg.top_n_traders:
                break
            if w in followed or w in self.cfg.exclude_wallets:
                continue
            if w in self.leaderboard.wallets and self.leaderboard.is_scalper(self.leaderboard.wallets[w], now):
                continue
            followed.append(w)
        changed = set(followed) != self.followed
        self.followed = set(followed)
        if self.feed:
            await self.feed.set_accounts(self.followed)
        if changed:
            since = now - self.leaderboard.window_sec
            log.info("following %d traders (%d ranked)", len(self.followed), len(top))
            for i, s in enumerate(top[:10], 1):
                log.info(
                    "  #%d %s… pnl=%+.2f SOL trips=%d win=%.0f%% hold=%.0fs copy-hits=%d/%d",
                    i, s.wallet[:8], s.pnl(since), len(s.trips(since)), s.win_rate(since) * 100,
                    s.median_hold(since),
                    s.copy_hits, s.copy_hits + s.copy_misses,
                )

    # ------------------------------------------------------------ token subs
    def _needed_tokens(self) -> set[str]:
        return set(self.tracked_tokens) | set(self.positions.open) | self.outcomes.mints()

    async def _track_new_token(self, mint: str) -> None:
        self.tracked_tokens[mint] = self.clock()
        dropped = []
        while len(self.tracked_tokens) > self.cfg.max_tracked_tokens:
            old, _ = self.tracked_tokens.popitem(last=False)
            dropped.append(old)
        if self.feed:
            await self.feed.subscribe_tokens([mint])
            needed = self._needed_tokens()
            await self.feed.unsubscribe_tokens([m for m in dropped if m not in needed])

    async def _watch(self, mint: str) -> None:
        if self.feed:
            await self.feed.subscribe_tokens([mint])

    # ---------------------------------------------------------------- events
    def _dup(self, sig: str) -> bool:
        if not sig:
            return False
        if sig in self._seen_sigs:
            return True
        self._seen_sigs[sig] = None
        if len(self._seen_sigs) > 20000:
            self._seen_sigs.popitem(last=False)
        return False

    async def handle(self, msg: dict) -> None:
        evt = TradeEvent.from_pumpportal(msg, now=self.clock())
        if evt is None or self._dup(evt.signature):
            return
        if evt.side == "create":
            await self._track_new_token(evt.mint)

        self.prices[evt.mint] = evt.price
        self.leaderboard.on_trade(evt)

        for wallet, hit in self.outcomes.on_price(evt.mint, evt.price, evt.timestamp):
            s = self.leaderboard.get(wallet)
            if hit:
                s.copy_hits += 1
            else:
                s.copy_misses += 1

        pos = self.positions.open.get(evt.mint)
        if pos is not None:
            was_runner = pos.runner
            reason = self.positions.exit_reason(evt.mint, evt.price, evt.timestamp)
            if reason:
                await self._exit(evt.mint, reason)
            elif pos.runner and not was_runner:
                log.info(
                    "RUNNER %s… hit +%.0f%%: letting it run (trail %.0f%% from peak, floor +%.0f%%)",
                    evt.mint[:8], self.cfg.take_profit_pct, self.cfg.trailing_stop_pct, self.cfg.tp_floor_pct,
                )

        if evt.trader in self.followed:
            if evt.is_buy:
                await self._on_leader_buy(evt)
            elif self.cfg.exit_on_leader_sell:
                pos = self.positions.open.get(evt.mint)
                # runners are protected by the floor/trailing stop, so a leader
                # taking profit doesn't cut them short
                if pos is not None and not pos.runner and evt.trader in pos.leaders:
                    await self._exit(evt.mint, f"leader {evt.trader[:6]} sold")

    async def _on_leader_buy(self, evt: TradeEvent) -> None:
        self.outcomes.add(evt.trader, evt.mint, evt.price, evt.timestamp)
        await self._watch(evt.mint)
        sig = self.analyzer.evaluate(evt, self.prices.get(evt.mint, evt.price), self.followed)
        self.analyzer.record_buy(evt)
        log.info("signal %s… from %s…: %s", evt.mint[:8], evt.trader[:6], sig.summary())
        if not sig.approved or evt.mint in self._busy:
            return
        ok, why = self.positions.can_open(
            evt.mint, evt.timestamp, await self.executor.balance(), pending=len(self._buying)
        )
        if not ok:
            log.info("  not entering: %s", why)
            return
        self._busy.add(evt.mint)
        self._buying.add(evt.mint)
        self._spawn(self._do_buy(evt.mint, sig))

    # ------------------------------------------------------------ orders
    def _spawn(self, coro) -> None:
        """Orders run as tasks so the feed keeps flowing while we wait for confirmation."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _do_buy(self, mint: str, sig) -> None:
        try:
            fill = await self.executor.buy(mint, self.cfg.buy_amount_sol, self.prices.get(mint, sig.current_price))
            if not fill.ok:
                log.warning("  buy %s… failed: %s", mint[:8], fill.error)
                self.positions.last_trade_at[mint] = self.clock()  # don't hammer a failing mint
                return
            self.positions.add(Position(mint, fill.price, fill.sol, fill.tokens, self.clock(), sig.leaders, sig.probability))
            log.info(
                "BUY %s %.4f SOL -> %.0f tokens @ %.3e (signal %.3e, TP %.3e / SL %.3e) tx=%s",
                mint, fill.sol, fill.tokens, fill.price, sig.current_price,
                fill.price * (1 + self.cfg.take_profit_pct / 100),
                fill.price * (1 - self.cfg.stop_loss_pct / 100), fill.signature,
            )
        except Exception:
            log.exception("buy %s… crashed", mint[:8])
        finally:
            self._busy.discard(mint)
            self._buying.discard(mint)

    async def _exit(self, mint: str, reason: str) -> None:
        if mint not in self.positions.open or mint in self._busy:
            return
        self._busy.add(mint)
        self._spawn(self._do_exit(mint, reason))

    async def _do_exit(self, mint: str, reason: str) -> None:
        try:
            pos = self.positions.open.get(mint)
            if pos is None:
                return
            fill = await self.executor.sell(mint, pos.tokens, self.prices.get(mint, pos.last_price))
            if not fill.ok:
                log.warning("sell %s… failed: %s (will retry)", mint[:8], fill.error)
                self._exit_retry[mint] = reason
                return
            self._exit_retry.pop(mint, None)
            trade = self.positions.close(mint, fill.sol, self.clock(), reason)
            log.info(
                "SELL %s… %s pnl=%+.4f SOL (%+.1f%%) tx=%s",
                mint[:8], reason, trade.pnl_sol, trade.pnl_sol / trade.sol_in * 100, fill.signature,
            )
        except Exception:
            log.exception("sell %s… crashed (will retry)", mint[:8])
            self._exit_retry[mint] = reason
        finally:
            self._busy.discard(mint)

    # ------------------------------------------------------------ maintenance
    async def tick(self) -> None:
        now = self.clock()
        for wallet, _ in self.outcomes.expire(now):
            self.leaderboard.get(wallet).copy_misses += 1
        for mint, reason in list(self._exit_retry.items()):
            await self._exit(mint, reason)
        for mint in list(self.positions.open):
            reason = self.positions.exit_reason(mint, 0.0, now)
            if reason:
                await self._exit(mint, reason)
        self.analyzer.prune(now)

    async def maintenance(self) -> None:
        now = self.clock()
        self.leaderboard.prune(now)
        await self.refresh_followed()
        if self.feed:
            needed = self._needed_tokens()
            await self.feed.unsubscribe_tokens([m for m in list(self.feed.tokens) if m not in needed])
        self.save_state()
        st = self.positions.stats()
        log.info(
            "status: balance=%.3f SOL open=%d closed=%d win=%.0f%% pnl=%+.4f SOL wallets=%d pending-outcomes=%d",
            await self.executor.balance(), st["open"], st["closed"], st["win_rate"] * 100,
            st["pnl_sol"], len(self.leaderboard.wallets), len(self.outcomes),
        )

    async def run(self) -> None:
        from .feed import PumpPortalFeed

        self.load_state()
        if not self.cfg.api_key:
            log.warning(
                "PUMPPORTAL_API_KEY not set: PumpPortal only streams new-token events without a "
                "funded API key, so traders cannot be ranked or followed. See README."
            )
        if self.feed is None:
            self.feed = PumpPortalFeed(self.cfg.ws_endpoint, self.handle)
        await self.feed.subscribe_tokens(self.positions.open)
        await self.refresh_followed()
        feed_task = asyncio.create_task(self.feed.run())
        last_maint = self.clock()
        try:
            while not feed_task.done():
                await asyncio.sleep(5)
                await self.tick()
                if self.clock() - last_maint >= self.cfg.leaderboard_refresh_sec:
                    await self.maintenance()
                    last_maint = self.clock()
        finally:
            self.feed.stop()
            feed_task.cancel()
            await self.drain()
            self.save_state()
            await self.executor.close()
