"""Ensemble strategy (STRATEGY=ensemble, default): all tactics together, the best ones trade.

Every tactic in tactics.py runs all the time as a *shadow*: it watches every
new coin and trades on its own simulated wallet. From those simulated trades
the ensemble keeps a live ranking of which tactics are making money **right
now**, and the real wallet follows the buy signals of the top ENSEMBLE_TOP_K.

Ranking (an "upper confidence bound", re-computed every minute):

    score = sum of net % results in the window / (trades + 1)
          + ENSEMBLE_EXPLORE_BONUS / sqrt(trades + 1)

* a tactic that keeps winning rises to the top and trades for real
* a tactic that keeps losing drops out automatically
* a tactic with few trades gets a small bonus, so new or quiet tactics still
  get tried. The bonus shrinks as evidence comes in.

When a ranked tactic buys, the real wallet buys the same coin at the same
moment. That position then follows that tactic's exits. One coin is held by
one tactic at a time. Shadows keep trading whether or not they rank, so the
ranking is always based on fresh results.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import replace
from functools import partial
from typing import Any

from .base import BaseBot
from .config import Config
from .executor import PaperExecutor
from .market import MarketData
from .mux import FeedProxy, NullFeed
from .scalper import ScalperBot
from .socials import SocialsChecker
from .tactics import TACTICS


class _MasterMux:
    """Lets shadow tactics subscribe through the master bot's single feed."""

    def __init__(self, master: "EnsembleBot"):
        self.master = master
        self.proxies: list[FeedProxy] = []
        self._null = NullFeed()

    @property
    def feed(self):
        return self.master.feed or self._null


def resolve_tactics(wanted: list[str], tactics: dict[str, dict]) -> list[str]:
    """'all', 'est' (established-coin tactics), 'new' (new-coin tactics) or explicit names."""
    names: list[str] = []
    for w in wanted:
        if w == "all":
            picked = list(tactics)
        elif w == "est":
            picked = [n for n in tactics if n.startswith("est_")]
        elif w == "new":
            picked = [n for n in tactics if not n.startswith("est_")]
        else:
            picked = [w] if w in tactics else []
        names += [n for n in picked if n not in names]
    return names


def tactic_score(pcts: list[float], bonus: float) -> float:
    n = len(pcts)
    return sum(pcts) / (n + 1) + bonus / math.sqrt(n + 1)


class EnsembleBot(BaseBot):
    def __init__(
        self, cfg: Config, executor, feed=None, clock=time.time, sleep=asyncio.sleep,
        socials: SocialsChecker | None = None, tactics: dict[str, dict] | None = None,
        market: MarketData | None = None,
    ):
        # the real wallet follows the tactics' exits; its own rules are only safety nets
        mcfg = replace(
            cfg,
            max_open_positions=cfg.ensemble_max_open_positions,
            mint_cooldown_sec=0,
            max_hold_sec=cfg.ensemble_safety_max_hold_sec,
        )
        super().__init__(cfg, executor, feed, clock, positions_cfg=mcfg)
        self.socials = socials or SocialsChecker(cfg.ipfs_gateways)
        self._own_market = market is None
        self.market = market or MarketData(cfg)
        tactics = tactics or TACTICS
        names = resolve_tactics(cfg.ensemble_tactics, tactics)
        if not names:
            raise ValueError(f"ENSEMBLE_TACTICS matched nothing; choose from {', '.join(tactics)}")
        os.makedirs(cfg.ensemble_state_dir, exist_ok=True)
        self._mux = _MasterMux(self)
        self._own_feed = FeedProxy(self._mux)  # keeps prices flowing for coins we hold
        self._mux.proxies.append(self._own_feed)
        self.shadows: dict[str, ScalperBot] = {}
        shadow_log_level = logging.DEBUG if cfg.log_level.upper() == "DEBUG" else logging.WARNING
        for name in names:
            tcfg = replace(
                cfg, **tactics[name], dry_run=True, max_daily_loss_sol=1e9,
                state_file=os.path.join(cfg.ensemble_state_dir, f"{name}.json"),
            )
            proxy = FeedProxy(self._mux)
            self._mux.proxies.append(proxy)
            bot = ScalperBot(tcfg, PaperExecutor(tcfg, sleep=sleep), feed=proxy, clock=clock, socials=self.socials,
                             market=self.market)
            bot.log = logging.getLogger(f"pumpbot.shadow.{name}")
            bot.log.setLevel(shadow_log_level)
            bot.enter_hooks.append(partial(self._on_tactic_buy, name))
            bot.exit_hooks.append(partial(self._on_tactic_exit, name))
            self.shadows[name] = bot
        self.owner: dict[str, str] = {}  # mint -> tactic that the real position follows
        self.active: list[str] = []
        self.scores: dict[str, tuple[float, int, float]] = {}  # name -> (score, trades, avg %)
        self._exit_after_buy: dict[str, str] = {}
        self._last_rank = -1e18

    # ------------------------------------------------------------- ranking
    def rank(self) -> None:
        now = self.clock()
        since = now - self.cfg.ensemble_window_min * 60
        for name, bot in self.shadows.items():
            pcts = [t.pnl_sol / t.sol_in * 100 for t in bot.positions.closed if t.closed_at >= since and t.sol_in]
            avg = sum(pcts) / len(pcts) if pcts else 0.0
            self.scores[name] = (tactic_score(pcts, self.cfg.ensemble_explore_bonus), len(pcts), avg)
        order = sorted(self.scores, key=lambda n: (-self.scores[n][0], self.scores[n][1]))
        active = [n for n in order if self.scores[n][0] > self.cfg.ensemble_min_score][: self.cfg.ensemble_top_k]
        if active != self.active:
            self.log.info(
                "ENSEMBLE ranking -> trading with: %s",
                ", ".join(f"{n} (score {self.scores[n][0]:+.1f}, {self.scores[n][1]} trades, avg {self.scores[n][2]:+.1f}%)"
                          for n in active) or "nobody (every tactic is losing)",
            )
        self.active = active
        self._last_rank = now

    # --------------------------------------------------------------- hooks
    async def _on_tactic_buy(self, name: str, bot, mint: str, price: float, probability: float, label: str) -> None:
        if name not in self.active or mint in self.positions.open or mint in self._busy:
            return
        if await self._enter(mint, price, self.clock(), [name], probability, f"{name}: {label}"):
            self.owner[mint] = name

    async def _on_tactic_exit(self, name: str, bot, mint: str, reason: str) -> None:
        if self.owner.get(mint) != name:
            return
        if mint in self._buying:
            self._exit_after_buy[mint] = f"[{name}] {reason}"
            return
        await self._exit(mint, f"[{name}] {reason}")

    def on_bought(self, mint: str) -> None:
        self._spawn(self._own_feed.subscribe_tokens([mint]))
        reason = self._exit_after_buy.pop(mint, None)
        if reason:
            self._spawn(self._exit(mint, reason))

    # -------------------------------------------------------------- events
    async def handle(self, msg: dict) -> None:
        evt = self.parse(msg)
        if evt is not None:
            pos = self.positions.open.get(evt.mint)
            if pos is not None and evt.price > 0:
                pos.last_price = evt.price
                pos.peak_price = max(pos.peak_price, evt.price)
        for bot in self.shadows.values():
            try:
                await bot.handle(msg)
            except Exception:
                bot.log.exception("shadow handler error")

    async def tick(self) -> None:
        now = self.clock()
        for bot in self.shadows.values():
            await bot.tick()
        for mint, reason in list(self._exit_retry.items()):
            await self._exit(mint, reason)
        for mint, pos in list(self.positions.open.items()):
            if mint in self._busy:
                continue
            owner = self.owner.get(mint)
            shadow = self.shadows.get(owner) if owner else None
            if now - pos.opened_at >= self.cfg.ensemble_safety_max_hold_sec:
                await self._exit(mint, "safety timeout")
            elif shadow is None or (mint not in shadow.positions.open and mint not in shadow._busy):
                # the tactic's own (simulated) buy failed or it already sold
                await self._exit(mint, f"[{owner}] tactic no longer holds")
        done = [m for m in self.owner if m not in self.positions.open and m not in self._busy]
        for mint in done:
            del self.owner[mint]
        if done:
            await self._own_feed.unsubscribe_tokens(done)
        if now - self._last_rank >= 60:
            self.rank()

    # --------------------------------------------------------------- state
    def state_extra(self) -> dict[str, Any]:
        return {"owner": self.owner, "active": self.active}

    def load_extra(self, data: dict[str, Any]) -> None:
        self.owner.update(data.get("owner", {}))

    async def on_start(self) -> None:
        for bot in self.shadows.values():
            bot.load_state()
            await bot.feed.subscribe_tokens(bot.positions.open)
        self.rank()

    def status_extra(self) -> str:
        coins = max((b.counts["coins"] for b in self.shadows.values()), default=0)
        trades = sum(len(b.positions.closed) for b in self.shadows.values())
        board = ", ".join(f"{n} {self.scores[n][0]:+.1f}" for n in self.active)
        return f"coins={coins} shadow-trades={trades} | trading with: {board or 'nobody'}"

    async def maintenance(self) -> None:
        for bot in self.shadows.values():
            await bot.maintenance()
        await super().maintenance()

    async def drain_all(self) -> None:
        while self._tasks or any(b._tasks for b in self.shadows.values()):
            await self.drain()
            for b in self.shadows.values():
                await b.drain()

    async def run(self) -> None:
        try:
            await super().run()
        finally:
            for bot in self.shadows.values():
                await bot.drain()
                bot.save_state()
            await self.socials.close()
            if self._own_market:
                await self.market.close()
