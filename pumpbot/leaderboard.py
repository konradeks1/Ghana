"""Discovers the most profitable Pump.fun traders from the live trade stream.

Every buy/sell seen on tracked tokens is attributed to the trading wallet.
Realised PnL is computed per wallet (average-cost basis per mint), and the
wallets are ranked by realised profit over a rolling window.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .models import TradeEvent

DUST = 0.01  # a lot with <1% of tokens left counts as closed


@dataclass
class Lot:
    tokens: float = 0.0
    cost_sol: float = 0.0
    proceeds_sol: float = 0.0
    total_cost_sol: float = 0.0
    opened_at: float = 0.0


@dataclass
class WalletStats:
    wallet: str
    realized: deque = field(default_factory=deque)  # (ts, pnl_sol)
    round_trips: deque = field(default_factory=deque)  # (ts, return_pct, hold_sec)
    buy_sizes: deque = field(default_factory=lambda: deque(maxlen=50))
    open_lots: dict[str, Lot] = field(default_factory=dict)
    copy_hits: int = 0  # entries that reached +TP before -SL (see outcomes.py)
    copy_misses: int = 0
    last_seen: float = 0.0

    # ---- aggregates ---------------------------------------------------------
    def pnl(self, since: float = 0.0) -> float:
        return sum(p for ts, p in self.realized if ts >= since)

    def trips(self, since: float = 0.0) -> list[tuple[float, float, float]]:
        return [t for t in self.round_trips if t[0] >= since]

    def win_rate(self, since: float = 0.0) -> float:
        trips = self.trips(since)
        return sum(1 for _, r, _ in trips if r > 0) / len(trips) if trips else 0.0

    def tp_rate(self, tp_pct: float, since: float = 0.0) -> tuple[int, int]:
        trips = self.trips(since)
        return sum(1 for _, r, _ in trips if r >= tp_pct), len(trips)

    def median_buy(self) -> float:
        return statistics.median(self.buy_sizes) if self.buy_sizes else 0.0

    def median_hold(self) -> float:
        holds = [h for _, _, h in self.round_trips]
        return statistics.median(holds) if holds else 0.0

    # ---- persistence ----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "realized": list(self.realized),
            "round_trips": list(self.round_trips),
            "buy_sizes": list(self.buy_sizes),
            "copy_hits": self.copy_hits,
            "copy_misses": self.copy_misses,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WalletStats":
        s = cls(wallet=d["wallet"])
        s.realized = deque(tuple(x) for x in d.get("realized", []))
        s.round_trips = deque(tuple(x) for x in d.get("round_trips", []))
        s.buy_sizes.extend(d.get("buy_sizes", []))
        s.copy_hits = d.get("copy_hits", 0)
        s.copy_misses = d.get("copy_misses", 0)
        s.last_seen = d.get("last_seen", 0.0)
        return s


class Leaderboard:
    def __init__(self, window_hours: float = 24.0, min_round_trips: int = 3, exclude: set[str] | None = None):
        self.window_sec = window_hours * 3600
        self.min_round_trips = min_round_trips
        self.exclude = exclude or set()
        self.wallets: dict[str, WalletStats] = {}

    def get(self, wallet: str) -> WalletStats:
        s = self.wallets.get(wallet)
        if s is None:
            s = self.wallets[wallet] = WalletStats(wallet)
        return s

    def on_trade(self, evt: TradeEvent) -> None:
        if evt.trader in self.exclude:
            return
        s = self.get(evt.trader)
        s.last_seen = evt.timestamp
        if evt.is_buy:
            if evt.sol_amount <= 0 or evt.token_amount <= 0:
                return
            lot = s.open_lots.get(evt.mint)
            if lot is None:
                lot = s.open_lots[evt.mint] = Lot(opened_at=evt.timestamp)
            lot.tokens += evt.token_amount
            lot.cost_sol += evt.sol_amount
            lot.total_cost_sol += evt.sol_amount
            s.buy_sizes.append(evt.sol_amount)
            return

        lot = s.open_lots.get(evt.mint)
        if lot is None or lot.tokens <= 0:
            return  # bought before we started watching: cost basis unknown
        sold = min(evt.token_amount, lot.tokens)
        frac = sold / lot.tokens
        cost_part = lot.cost_sol * frac
        # scale proceeds if they sold more than we know they held
        proceeds = evt.sol_amount * (sold / evt.token_amount) if evt.token_amount > 0 else 0.0
        s.realized.append((evt.timestamp, proceeds - cost_part))
        remaining_frac = 1 - frac
        lot.tokens -= sold
        lot.cost_sol -= cost_part
        lot.proceeds_sol += proceeds
        if remaining_frac < DUST:
            ret = (lot.proceeds_sol / lot.total_cost_sol - 1) * 100 if lot.total_cost_sol > 0 else 0.0
            s.round_trips.append((evt.timestamp, ret, evt.timestamp - lot.opened_at))
            del s.open_lots[evt.mint]

    def prune(self, now: float, max_open_age_sec: float = 6 * 3600) -> None:
        cutoff = now - self.window_sec
        for w in list(self.wallets):
            s = self.wallets[w]
            while s.realized and s.realized[0][0] < cutoff:
                s.realized.popleft()
            while s.round_trips and s.round_trips[0][0] < cutoff:
                s.round_trips.popleft()
            for m in [m for m, lot in s.open_lots.items() if now - lot.opened_at > max_open_age_sec]:
                del s.open_lots[m]
            if not s.realized and not s.open_lots and now - s.last_seen > self.window_sec:
                del self.wallets[w]

    def top(self, n: int, now: float) -> list[WalletStats]:
        since = now - self.window_sec
        ranked = [
            s
            for s in self.wallets.values()
            if s.wallet not in self.exclude
            and len(s.trips(since)) >= self.min_round_trips
            and s.pnl(since) > 0
        ]
        ranked.sort(key=lambda s: s.pnl(since), reverse=True)
        return ranked[:n]

    def to_dict(self) -> dict[str, Any]:
        return {w: s.to_dict() for w, s in self.wallets.items() if s.realized or s.copy_hits or s.copy_misses}

    def load(self, data: dict[str, Any]) -> None:
        for w, d in data.items():
            self.wallets[w] = WalletStats.from_dict(d)
