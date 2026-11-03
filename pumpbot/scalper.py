"""Scalp strategy (STRATEGY=scalp, default): watch every new coin and scalp the strong ones.

1. Every new Pump.fun coin is tracked from its first trade.
2. Its metadata is fetched; coins without website / X / Instagram are never bought.
3. Every trade updates holders, dev behaviour and activity; rug signs flag
   the coin (and blacklist its creator when the dev dumps).
4. Coins with lots of trades, many traders, strong buying and rising activity
   get a potential score; high scorers are bought.
5. Exits: +TP (then trail as a runner), stop loss, short max hold, momentum
   fade, dev selling, or a whale dump. The same coin can be scalped again
   after a cooldown.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import replace
from typing import Any

from .base import BaseBot
from .config import Config
from .potential import PotentialAnalyzer
from .socials import SocialsChecker, extract_socials
from .tokens import TokenState

log = logging.getLogger("pumpbot")


def _reason_key(reason: str) -> str:
    return re.sub(r"[-+]?\d+(\.\d+)?", "#", reason.split(":")[0] if reason.startswith("RUG") else reason)


class ScalperBot(BaseBot):
    def __init__(self, cfg: Config, executor, feed=None, clock=time.time, socials: SocialsChecker | None = None):
        pcfg = replace(
            cfg,
            max_hold_sec=cfg.scalp_max_hold_sec,
            mint_cooldown_sec=cfg.scalp_reentry_cooldown_sec,
            max_open_positions=cfg.scalp_max_open_positions,
        )
        super().__init__(cfg, executor, feed, clock, positions_cfg=pcfg)
        self.analyzer = PotentialAnalyzer(cfg)
        self.socials = socials or SocialsChecker(cfg.ipfs_gateways)
        self.tokens: OrderedDict[str, TokenState] = OrderedDict()
        self.creator_launches: dict[str, deque] = {}
        self.bad_creators: dict[str, str] = {}
        self.skip_reasons: Counter = Counter()
        self.counts: Counter = Counter()

    # ---------------------------------------------------------------- state
    def state_extra(self) -> dict[str, Any]:
        return {"bad_creators": self.bad_creators}

    def load_extra(self, data: dict[str, Any]) -> None:
        self.bad_creators.update(data.get("bad_creators", {}))
        self.log.info("restored %d blacklisted creators", len(self.bad_creators))

    # --------------------------------------------------------------- events
    async def handle(self, msg: dict) -> None:
        evt = self.parse(msg)
        if evt is None:
            return
        if evt.side == "create":
            await self._on_create(evt.mint, evt.trader, evt.timestamp, msg)

        t = self.tokens.get(evt.mint)
        if t is None:
            await self._update_position(evt)
            return
        t.on_trade(evt)

        if not evt.is_buy and evt.trader == t.creator and not t.flag:
            self._flag(t, f"dev sold {t.pct(evt.token_amount):.1f}% of supply", blacklist=True)

        if evt.mint in self.positions.open:
            reason = self.analyzer.exit_signal(t, evt.trader, evt.token_amount, evt.is_buy, evt.timestamp)
            if reason:
                self.positions.exit_reason(evt.mint, evt.price, evt.timestamp)  # update last price
                await self._exit(evt.mint, reason)
            else:
                await self._update_position(evt)
            return

        await self._maybe_enter(t, evt.timestamp)

    def _flag(self, t: TokenState, why: str, blacklist: bool = False) -> None:
        t.flag = why
        self.counts["rug_flags"] += 1
        if blacklist:
            self.bad_creators[t.creator] = f"{why} on {t.symbol or t.mint[:8]}"
        self.log.info("RUG %s… (%s): %s%s", t.mint[:8], t.symbol, why, " — creator blacklisted" if blacklist else "")

    async def _on_create(self, mint: str, creator: str, ts: float, msg: dict) -> None:
        t = TokenState(
            mint, creator, ts, str(msg.get("name", ""))[:40], str(msg.get("symbol", ""))[:16], str(msg.get("uri", ""))
        )
        self.counts["coins"] += 1
        launches = self.creator_launches.setdefault(creator, deque())
        launches.append(ts)
        while launches and ts - launches[0] > 3600:
            launches.popleft()
        if creator in self.bad_creators:
            t.flag = f"creator blacklisted ({self.bad_creators[creator]})"
        elif len(launches) > self.cfg.rug_max_creator_launches_per_hour:
            t.flag = f"serial launcher: {len(launches)} coins in 1h"
        self.tokens[mint] = t
        await self._prune_tokens(ts, force_cap=True)
        if self.feed:
            await self.feed.subscribe_tokens([mint])
        if not t.flag:
            self._spawn(self._load_socials(t))

    async def _load_socials(self, t: TokenState) -> None:
        try:
            meta = await self.socials.fetch_metadata(t.uri)
        except Exception as e:  # pragma: no cover - network
            self.log.debug("metadata %s failed: %s", t.mint[:8], e)
            meta = None
        if meta is None:
            t.socials, t.socials_note = {}, "metadata unavailable"
            self.counts["metadata_failed"] += 1
            return
        found = extract_socials(meta)
        reused = self.socials.reused_links(t.mint, found)
        t.socials = found
        if found:
            self.counts["with_socials"] += 1
        if reused:
            self._flag(t, "reuses links of another coin: " + ", ".join(reused))

    async def _maybe_enter(self, t: TokenState, now: float) -> None:
        c = self.cfg
        if t.flag or t.mint in self._busy or t.entries >= c.scalp_max_entries_per_token:
            return
        age = now - t.created_at
        if age < c.scalp_min_age_sec or age > c.scalp_max_age_sec:
            return
        if now - t.last_eval < c.scalp_eval_interval_sec:
            return
        t.last_eval = now
        a = self.analyzer.assess(t, now)
        if a.rug and not t.flag:
            self._flag(t, a.rug, blacklist=a.rug.startswith("dev sold"))
        if not a.approved:
            self.skip_reasons[_reason_key(a.reasons[0])] += 1
            self.log.debug("skip %s", a.summary())
            return
        self.log.info("signal %s (%s) %s", t.symbol, ", ".join(f"{k}" for k in (t.socials or {})), a.summary())
        if await self._enter(t.mint, t.last_price, now, [], a.score / 100, f"{t.symbol} score {a.score:.0f}"):
            t.entries += 1
            self.counts["entries"] += 1

    # ---------------------------------------------------------- maintenance
    async def _prune_tokens(self, now: float, force_cap: bool = False) -> None:
        keep_after = now - self.cfg.scalp_max_age_sec - 300
        drop = []
        for mint, t in self.tokens.items():
            if mint in self.positions.open or mint in self._busy:
                continue
            if t.created_at < keep_after:
                drop.append(mint)
        over = len(self.tokens) - len(drop) - self.cfg.max_tracked_tokens
        if force_cap and over > 0:
            for mint in self.tokens:  # oldest first
                if over <= 0:
                    break
                if mint not in drop and mint not in self.positions.open and mint not in self._busy:
                    drop.append(mint)
                    over -= 1
        for mint in drop:
            del self.tokens[mint]
        if drop and self.feed:
            await self.feed.unsubscribe_tokens(drop)

    async def tick(self) -> None:
        await super().tick()
        await self._prune_tokens(self.clock())

    def status_extra(self) -> str:
        top = ", ".join(f"{k} ×{v}" for k, v in self.skip_reasons.most_common(4))
        return (
            f"coins={self.counts['coins']} watching={len(self.tokens)} with-socials={self.counts['with_socials']} "
            f"rug-flags={self.counts['rug_flags']} entries={self.counts['entries']} | top skips: {top}"
        )

    async def maintenance(self) -> None:
        await super().maintenance()
        self.skip_reasons.clear()

    async def run(self) -> None:
        try:
            await super().run()
        finally:
            await self.socials.close()
