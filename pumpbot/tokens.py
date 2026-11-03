"""Live per-coin state built from the trade stream: activity, holders, dev behaviour."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .models import TradeEvent

TOTAL_SUPPLY = 1_000_000_000.0  # every pump.fun coin


@dataclass
class Window:
    trades: int = 0
    buys: int = 0
    sells: int = 0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    traders: int = 0
    buyers: int = 0
    low: float = 0.0
    first_half_trades: int = 0
    second_half_trades: int = 0

    @property
    def volume_sol(self) -> float:
        return self.buy_sol + self.sell_sol

    @property
    def buy_ratio(self) -> float:
        return self.buy_sol / self.volume_sol if self.volume_sol > 0 else 0.0

    @property
    def net_inflow_sol(self) -> float:
        return self.buy_sol - self.sell_sol

    @property
    def diversity(self) -> float:
        return self.traders / self.trades if self.trades else 0.0

    @property
    def acceleration(self) -> float:
        """Trades in the latest half of the window vs the earlier half."""
        return self.second_half_trades / max(1, self.first_half_trades)


@dataclass
class TokenState:
    mint: str
    creator: str
    created_at: float
    name: str = ""
    symbol: str = ""
    uri: str = ""
    trades: deque = field(default_factory=lambda: deque(maxlen=5000))  # (ts, is_buy, sol, trader, tokens, price)
    balances: dict[str, float] = field(default_factory=dict)
    dev_bought: float = 0.0
    dev_sold: float = 0.0
    early_tokens: float = 0.0  # bought in the first 3 seconds
    last_price: float = 0.0
    market_cap_sol: float = 0.0
    socials: dict[str, str] | None = None  # None until metadata is fetched
    socials_note: str = ""
    flag: str = ""  # permanent rug flag: never buy this coin
    entries: int = 0
    last_eval: float = 0.0

    def on_trade(self, evt: TradeEvent) -> None:
        self.trades.append((evt.timestamp, evt.is_buy, evt.sol_amount, evt.trader, evt.token_amount, evt.price))
        self.last_price = evt.price
        if evt.market_cap_sol:
            self.market_cap_sol = evt.market_cap_sol
        delta = evt.token_amount if evt.is_buy else -evt.token_amount
        self.balances[evt.trader] = max(0.0, self.balances.get(evt.trader, 0.0) + delta)
        if evt.trader == self.creator:
            if evt.is_buy:
                self.dev_bought += evt.token_amount
            else:
                self.dev_sold += evt.token_amount
        if evt.is_buy and evt.timestamp - self.created_at <= 3.0:
            self.early_tokens += evt.token_amount

    # ---------------------------------------------------------- holders
    def pct(self, tokens: float) -> float:
        return tokens / TOTAL_SUPPLY * 100

    @property
    def dev_pct(self) -> float:
        return self.pct(self.balances.get(self.creator, 0.0))

    def top_holders_pct(self, n: int = 10) -> float:
        return self.pct(sum(sorted(self.balances.values(), reverse=True)[:n]))

    def max_holder(self, exclude: set[str] | None = None) -> tuple[str, float]:
        best, amt = "", 0.0
        for w, b in self.balances.items():
            if exclude and w in exclude:
                continue
            if b > amt:
                best, amt = w, b
        return best, self.pct(amt)

    @property
    def early_supply_pct(self) -> float:
        return self.pct(self.early_tokens)

    # ---------------------------------------------------------- activity
    def window(self, now: float, sec: float) -> Window:
        w = Window()
        start, mid = now - sec, now - sec / 2
        traders, buyers = set(), set()
        low = 0.0
        for ts, is_buy, sol, trader, _tok, price in reversed(self.trades):
            if ts < start:
                break
            w.trades += 1
            traders.add(trader)
            if is_buy:
                w.buys += 1
                w.buy_sol += sol
                buyers.add(trader)
            else:
                w.sells += 1
                w.sell_sol += sol
            if ts >= mid:
                w.second_half_trades += 1
            else:
                w.first_half_trades += 1
            low = price if low == 0 else min(low, price)
        w.traders, w.buyers, w.low = len(traders), len(buyers), low
        return w
