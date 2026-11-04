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
    peak_price: float = 0.0
    runner: bool = False  # True once +TP was reached and we're letting it run
    runner_since: float = 0.0

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

    def can_open(self, mint: str, now: float, balance_sol: float, pending: int = 0) -> tuple[bool, str]:
        if mint in self.open:
            return False, "already holding"
        if len(self.open) + pending >= self.cfg.max_open_positions:
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
        pos.peak_price = max(pos.peak_price, pos.entry_price)
        self.open[pos.mint] = pos
        self.last_trade_at[pos.mint] = pos.opened_at

    def exit_reason(self, mint: str, price: float, now: float) -> str | None:
        """Exit rules.

        With ANY_PROFIT_MODE (default) the "TP" is any net gain: once a trade is
        up ANY_PROFIT_ARM_PCT after estimated selling costs it is locked in, and
        it never sells below ANY_PROFIT_FLOOR_PCT net. Otherwise TAKE_PROFIT_PCT
        and TP_FLOOR_PCT (e.g. the old +20% rule) apply.

        Before +TP: stop loss and timeout apply.
        At +TP: by default the trade becomes a "runner". There is no cap on the
        upside; a trailing stop follows the peak and the exit never goes below
        +TP_FLOOR_PCT. With LET_WINNERS_RUN=false it sells right at +TP.
        """
        cfg = self.cfg
        pos = self.open.get(mint)
        if pos is None:
            return None
        if price > 0:
            pos.last_price = price
            pos.peak_price = max(pos.peak_price, price)
        pnl = pos.pnl_pct(pos.last_price)
        eps = 1e-9

        if cfg.any_profit_mode:
            # thresholds are NET gains: add the estimated cost of selling
            tp_pct = cfg.any_profit_arm_pct + cfg.exit_cost_pct
            floor_pct = cfg.any_profit_floor_pct + cfg.exit_cost_pct
        else:
            tp_pct, floor_pct = cfg.take_profit_pct, cfg.tp_floor_pct

        if not pos.runner:
            if pnl >= tp_pct - eps:
                if not cfg.let_winners_run:
                    return f"take-profit {pnl:+.1f}%"
                pos.runner, pos.runner_since = True, now
                return None
            if pnl <= -cfg.stop_loss_pct + eps:
                return f"stop-loss {pnl:+.1f}%"
            if now - pos.opened_at >= cfg.max_hold_sec:
                return f"timeout {pnl:+.1f}%"
            return None

        # runner: trail the peak, never give back below the profit floor
        peak_pnl = pos.pnl_pct(pos.peak_price)
        trail_stop = pos.peak_price * (1 - cfg.trailing_stop_pct / 100)
        floor = pos.entry_price * (1 + floor_pct / 100)
        stop = max(trail_stop, floor)
        if pos.last_price <= stop * (1 + eps):
            kind = "floor" if floor >= trail_stop else "trailing-stop"
            return f"runner {kind} {pnl:+.1f}% (peak {peak_pnl:+.1f}%)"
        if now - pos.runner_since >= cfg.runner_max_hold_sec:
            return f"runner timeout {pnl:+.1f}% (peak {peak_pnl:+.1f}%)"
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
