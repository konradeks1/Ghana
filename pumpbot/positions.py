"""Open positions, exit rules (+TP / -SL / timeout) and risk limits."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Config


@dataclass
class Position:
    mint: str
    entry_price: float
    sol_in: float
    tokens: float
    opened_at: float
    leaders: list[str] = field(default_factory=list)
    probability: float = 0.0
    last_price: float = 0.0

    def pnl_pct(self, price: float) -> float:
        return (price / self.entry_price - 1) * 100


@dataclass
class ClosedTrade:
    mint: str
    sol_in: float
    sol_out: float
    opened_at: float
    closed_at: float
    reason: str

    @property
    def pnl_sol(self) -> float:
        return self.sol_out - self.sol_in


class PositionManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.open: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.last_trade_at: dict[str, float] = {}

    def daily_pnl(self, now: float) -> float:
        day_start = now - (now % 86400)
        return sum(t.pnl_sol for t in self.closed if t.closed_at >= day_start)

    def can_open(self, mint: str, now: float, balance_sol: float) -> tuple[bool, str]:
        if mint in self.open:
            return False, "already holding"
        if len(self.open) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if now - self.last_trade_at.get(mint, -1e18) < self.cfg.mint_cooldown_sec:
            return False, "mint cooldown"
        if self.daily_pnl(now) <= -self.cfg.max_daily_loss_sol:
            return False, "daily loss limit hit"
        if balance_sol < self.cfg.buy_amount_sol + self.cfg.priority_fee_sol:
            return False, "insufficient balance"
        return True, ""

    def add(self, pos: Position) -> None:
        pos.last_price = pos.entry_price
        self.open[pos.mint] = pos
        self.last_trade_at[pos.mint] = pos.opened_at

    def exit_reason(self, mint: str, price: float, now: float) -> str | None:
        pos = self.open.get(mint)
        if pos is None:
            return None
        if price > 0:
            pos.last_price = price
        pnl = pos.pnl_pct(pos.last_price)
        if pnl >= self.cfg.take_profit_pct - 1e-9:
            return f"take-profit {pnl:+.1f}%"
        if pnl <= -self.cfg.stop_loss_pct + 1e-9:
            return f"stop-loss {pnl:+.1f}%"
        if now - pos.opened_at >= self.cfg.max_hold_sec:
            return f"timeout {pnl:+.1f}%"
        return None

    def close(self, mint: str, sol_out: float, now: float, reason: str) -> ClosedTrade:
        pos = self.open.pop(mint)
        trade = ClosedTrade(mint, pos.sol_in, sol_out, pos.opened_at, now, reason)
        self.closed.append(trade)
        self.last_trade_at[mint] = now
        return trade

    def stats(self) -> dict[str, Any]:
        wins = [t for t in self.closed if t.pnl_sol > 0]
        return {
            "open": len(self.open),
            "closed": len(self.closed),
            "win_rate": len(wins) / len(self.closed) if self.closed else 0.0,
            "pnl_sol": sum(t.pnl_sol for t in self.closed),
            "today_sol": self.daily_pnl(time.time()),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "open": [asdict(p) for p in self.open.values()],
            "closed": [asdict(t) for t in self.closed[-500:]],
        }

    def load(self, data: dict[str, Any]) -> None:
        for p in data.get("open", []):
            self.open[p["mint"]] = Position(**p)
        self.closed = [ClosedTrade(**t) for t in data.get("closed", [])]
