"""Decides whether a followed trader's buy is worth copying.

For each buy by a top trader the analyzer estimates the probability that the
token reaches +TP% from *our* entry before hitting -SL%, then computes the
expected value of the trade after fees. Only trades with enough edge are taken.

Inputs to the estimate:
  * trader's measured hit rate on the exact TP/SL strategy (outcomes.py)
  * trader's historical share of round trips that returned >= TP%
  * how far price has already moved past the leader's entry (chasing)
  * confluence: other top traders buying the same token recently
  * conviction: leader's buy size vs. their usual size
  * market-cap band filter
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .leaderboard import Leaderboard, WalletStats
from .models import TradeEvent

PRIOR_HITS, PRIOR_MISSES = 2.0, 3.0  # Beta prior => 40% before any evidence
ROUND_TRIP_WEIGHT = 0.5  # round trips are weaker evidence than measured hits


@dataclass
class Signal:
    mint: str
    leader: str
    leader_price: float
    current_price: float
    probability: float = 0.0
    expected_value_pct: float = 0.0
    leaders: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    approved: bool = False

    def summary(self) -> str:
        verdict = "ENTER" if self.approved else "skip"
        return (
            f"{verdict} {self.mint[:8]}… p={self.probability:.2f} ev={self.expected_value_pct:+.1f}% "
            f"leaders={len(self.leaders)} | " + "; ".join(self.reasons)
        )


class EntryAnalyzer:
    def __init__(self, cfg: Config, leaderboard: Leaderboard):
        self.cfg = cfg
        self.lb = leaderboard
        # mint -> wallet -> (ts, price)
        self.recent_buys: dict[str, dict[str, tuple[float, float]]] = {}

    def trader_probability(self, s: WalletStats, now: float) -> float:
        since = now - self.lb.window_sec
        tp_wins, trips = s.tp_rate(self.cfg.take_profit_pct, since)
        hits = s.copy_hits + ROUND_TRIP_WEIGHT * tp_wins + PRIOR_HITS
        total = s.copy_hits + s.copy_misses + ROUND_TRIP_WEIGHT * trips + PRIOR_HITS + PRIOR_MISSES
        return hits / total

    def record_buy(self, evt: TradeEvent) -> None:
        buys = self.recent_buys.setdefault(evt.mint, {})
        buys.setdefault(evt.trader, (evt.timestamp, evt.price))

    def prune(self, now: float) -> None:
        cutoff = now - self.cfg.confluence_window_sec
        for mint in list(self.recent_buys):
            buys = {w: v for w, v in self.recent_buys[mint].items() if v[0] >= cutoff}
            if buys:
                self.recent_buys[mint] = buys
            else:
                del self.recent_buys[mint]

    def evaluate(self, evt: TradeEvent, current_price: float, followed: set[str]) -> Signal:
        cfg = self.cfg
        now = evt.timestamp
        sig = Signal(evt.mint, evt.trader, evt.price, current_price)

        # --- hard filters ---------------------------------------------------
        if evt.sol_amount < cfg.min_leader_buy_sol:
            sig.reasons.append(f"leader buy {evt.sol_amount:.2f} SOL < min")
            return sig
        if evt.market_cap_sol and not (cfg.min_market_cap_sol <= evt.market_cap_sol <= cfg.max_market_cap_sol):
            sig.reasons.append(f"mcap {evt.market_cap_sol:.0f} SOL outside band")
            return sig
        chase_pct = (current_price / evt.price - 1) * 100 if evt.price > 0 else 0.0
        if chase_pct > cfg.max_chase_pct:
            sig.reasons.append(f"price already +{chase_pct:.1f}% past leader entry")
            return sig

        # --- probability estimate --------------------------------------------
        cutoff = now - cfg.confluence_window_sec
        buyers = {
            w: v for w, v in self.recent_buys.get(evt.mint, {}).items() if w in followed and v[0] >= cutoff
        }
        buyers.setdefault(evt.trader, (now, evt.price))
        sig.leaders = sorted(buyers)

        probs = {w: self.trader_probability(self.lb.get(w), now) for w in buyers}
        p = probs[evt.trader]
        sig.reasons.append(f"leader p={p:.2f}")

        # confluence: each extra top trader removes part of the remaining doubt
        extra = len(buyers) - 1
        if extra > 0:
            p = 1 - (1 - max(probs.values())) * (1 - cfg.confluence_bonus) ** extra
            sig.reasons.append(f"+{extra} other top traders")

        # conviction: leader sizing up vs their usual buy
        med = self.lb.get(evt.trader).median_buy()
        if med > 0:
            ratio = evt.sol_amount / med
            if ratio >= 1.5:
                p += 0.03
                sig.reasons.append(f"size {ratio:.1f}x usual")
            elif ratio < 0.5:
                p -= 0.05
                sig.reasons.append(f"small size {ratio:.1f}x usual")

        # chasing: we enter higher than the leader, so the move we need
        # from the leader's entry is bigger than TP
        if chase_pct > 0:
            needed = ((1 + cfg.take_profit_pct / 100) * (1 + chase_pct / 100) - 1) * 100
            p *= cfg.take_profit_pct / needed
            sig.reasons.append(f"chasing +{chase_pct:.1f}%")

        p = max(0.0, min(0.95, p))
        ev = p * cfg.take_profit_pct - (1 - p) * cfg.stop_loss_pct - cfg.round_trip_cost_pct
        sig.probability, sig.expected_value_pct = p, ev
        sig.approved = p >= cfg.min_win_prob and ev >= cfg.min_expected_value_pct
        if not sig.approved:
            sig.reasons.append("edge too small")
        return sig
