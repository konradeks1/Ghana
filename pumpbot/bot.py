"""Copy-trade strategy (STRATEGY=copy): follow the top traders, take +TP exits."""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from typing import Any

from .analyzer import EntryAnalyzer
from .base import BaseBot
from .config import Config
from .leaderboard import Leaderboard
from .models import TradeEvent
from .outcomes import EntryOutcomeTracker

log = logging.getLogger("pumpbot")


class CopyTradeBot(BaseBot):
    def __init__(self, cfg: Config, executor, feed=None, clock=time.time):
        super().__init__(cfg, executor, feed, clock)
        self.maintenance_interval_sec = cfg.leaderboard_refresh_sec
        self.tick_interval_sec = 5.0
        self.leaderboard = Leaderboard(
            cfg.leaderboard_window_hours, cfg.min_round_trips, cfg.exclude_wallets, cfg.min_median_hold_sec
        )
        self.outcomes = EntryOutcomeTracker(cfg.take_profit_pct, cfg.stop_loss_pct, cfg.outcome_horizon_sec)
        self.analyzer = EntryAnalyzer(cfg, self.leaderboard)
        self.followed: set[str] = set()
        self.seed_traders: list[str] = self._load_seeds()
        self.tracked_tokens: OrderedDict[str, float] = OrderedDict()  # discovery set

    # ------------------------------------------------------------------ setup
    def _load_seeds(self) -> list[str]:
        path = self.cfg.seed_traders_file
        if not path or not os.path.exists(path):
            return []
        with open(path) as f:
            return [ln.split("#")[0].strip() for ln in f if ln.split("#")[0].strip()]

    def load_extra(self, data: dict[str, Any]) -> None:
        self.leaderboard.load(data.get("leaderboard", {}))
        log.info("restored %d wallets", len(self.leaderboard.wallets))

    def state_extra(self) -> dict[str, Any]:
        return {"leaderboard": self.leaderboard.to_dict(), "followed": sorted(self.followed)}

    async def on_start(self) -> None:
        await self.refresh_followed()

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
                    s.median_hold(since), s.copy_hits, s.copy_hits + s.copy_misses,
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

    # ---------------------------------------------------------------- events
    async def handle(self, msg: dict) -> None:
        evt = self.parse(msg)
        if evt is None:
            return
        if evt.side == "create":
            await self._track_new_token(evt.mint)

        self.leaderboard.on_trade(evt)
        for wallet, hit in self.outcomes.on_price(evt.mint, evt.price, evt.timestamp):
            s = self.leaderboard.get(wallet)
            if hit:
                s.copy_hits += 1
            else:
                s.copy_misses += 1

        await self._update_position(evt)

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
        if self.feed:
            await self.feed.subscribe_tokens([evt.mint])
        sig = self.analyzer.evaluate(evt, self.prices.get(evt.mint, evt.price), self.followed)
        self.analyzer.record_buy(evt)
        log.info("signal %s… from %s…: %s", evt.mint[:8], evt.trader[:6], sig.summary())
        if sig.approved:
            await self._enter(evt.mint, sig.current_price, evt.timestamp, sig.leaders, sig.probability, "copy")

    # ------------------------------------------------------------ maintenance
    async def tick(self) -> None:
        now = self.clock()
        for wallet, _ in self.outcomes.expire(now):
            self.leaderboard.get(wallet).copy_misses += 1
        await super().tick()
        self.analyzer.prune(now)

    def status_extra(self) -> str:
        return f"wallets={len(self.leaderboard.wallets)} pending-outcomes={len(self.outcomes)}"

    async def maintenance(self) -> None:
        self.leaderboard.prune(self.clock())
        await self.refresh_followed()
        if self.feed:
            needed = self._needed_tokens()
            await self.feed.unsubscribe_tokens([m for m in list(self.feed.tokens) if m not in needed])
        await super().maintenance()
