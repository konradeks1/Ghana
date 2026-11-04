"""Scores a new coin's short-term potential and screens it for rug-pull signs.

A coin is bought only if it passes EVERY hard check and its potential
score (0-100) is at least SCALP_MIN_SCORE.

Hard checks
  age        between SCALP_MIN_AGE_SEC and SCALP_MAX_AGE_SEC
  socials    has website / X / Instagram (REQUIRED_SOCIALS, SOCIALS_MODE)
  rug        flagged coin, blacklisted / serial creator, dev holds too much,
             dev sold, whales, bundled launch, wash trading, reused links
  activity   enough trades, unique traders, buy pressure and SOL inflow
  price      market cap in band, not already pumped too far

Score
  30  trade count       (the main signal: lots of trades)
  20  unique traders
  20  buy pressure
  15  net SOL inflow
  15  acceleration      (trading speeding up, not dying)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .socials import check_socials
from .tokens import TokenState


@dataclass
class Assessment:
    mint: str
    score: float = 0.0
    approved: bool = False
    reasons: list[str] = field(default_factory=list)  # why it was rejected
    rug: str = ""  # permanent rug flag raised by this check
    stats: str = ""

    def summary(self) -> str:
        verdict = "BUY" if self.approved else "skip"
        why = "; ".join(self.reasons) if self.reasons else "all checks passed"
        return f"{verdict} {self.mint[:8]}… score={self.score:.0f} | {self.stats} | {why}"


def _scale(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


class PotentialAnalyzer:
    def __init__(self, cfg: Config, market=None):
        self.cfg = cfg
        self.market = market  # MarketData: SOL price + RPC holder checks

    def mcap_usd(self, t: TokenState) -> float:
        if self.market is not None:
            return self.market.sol.mcap_usd(t.market_cap_sol)
        return t.market_cap_sol * self.cfg.sol_price_usd

    def rug_check(self, t: TokenState, now: float) -> tuple[str, str]:
        """(permanent_flag, temporary_reason). A permanent flag means never buy."""
        c = self.cfg
        if t.flag:
            return t.flag, ""
        if t.partial:
            # no launch history: use the RPC's top holders instead of our own ledger
            res = self.market.holders.get(t.mint, now) if self.market is not None else None
            if res is None:
                return "", "holders not checked yet" if c.require_holder_check else ""
            top10, mx, err = res
            if err or top10 is None:
                return "", ("holder check unavailable" if c.require_holder_check else "")
            if top10 > c.rug_max_top10_pct:
                return "", f"top-10 hold {top10:.0f}%"
            if mx > c.rug_max_single_holder_pct:
                return "", f"one wallet holds {mx:.1f}%"
            return "", ""
        if t.dev_sold > 0:
            return f"dev sold {t.pct(t.dev_sold):.1f}% of supply", ""
        if t.early_supply_pct > c.rug_max_early_supply_pct:
            return f"bundled launch: {t.early_supply_pct:.0f}% bought in first 3s", ""
        if now - t.created_at > 30 and t.hidden_supply_pct > c.rug_max_hidden_supply_pct:
            return "", f"{t.hidden_supply_pct:.0f}% of supply held by wallets that never bought (bundles/dev bags)"
        if t.dev_pct > c.rug_max_dev_pct:
            return "", f"dev holds {t.dev_pct:.1f}%"
        top10 = t.top_holders_pct(10)
        if top10 > c.rug_max_top10_pct:
            return "", f"top-10 hold {top10:.0f}%"
        whale, whale_pct = t.max_holder()
        if whale_pct > c.rug_max_single_holder_pct:
            return "", f"one wallet holds {whale_pct:.1f}%"
        return "", ""

    def assess(self, t: TokenState, now: float) -> Assessment:
        c = self.cfg
        a = Assessment(t.mint)
        age = now - t.created_at
        w = t.window(now, c.scalp_window_sec)
        a.stats = (
            f"age={age:.0f}s trades={w.trades} traders={w.traders} buy={w.buy_ratio:.0%} "
            f"net={w.net_inflow_sol:+.1f}SOL mcap={t.market_cap_sol:.0f}SOL (${self.mcap_usd(t) / 1000:.0f}k)"
        )

        # --- rug screening ---------------------------------------------------
        flag, rug_reason = self.rug_check(t, now)
        if flag:
            a.rug = flag
            a.reasons.append(f"RUG RISK: {flag}")
            return a
        if rug_reason:
            a.reasons.append(f"rug risk: {rug_reason}")

        # --- socials -----------------------------------------------------------
        ok, why = check_socials(t.socials, c.required_socials, c.socials_mode)
        if not ok:
            a.reasons.append(why)
        if t.socials_note:
            a.reasons.append(t.socials_note)

        # --- age / price band ----------------------------------------------------
        if age < c.scalp_min_age_sec:
            a.reasons.append("too new")
        elif age > c.scalp_max_age_sec:
            a.reasons.append("too old")
        if c.scalp_min_market_cap_usd > 0:
            usd = self.mcap_usd(t)
            if usd < c.scalp_min_market_cap_usd or (c.scalp_max_market_cap_usd and usd > c.scalp_max_market_cap_usd):
                a.reasons.append("mcap outside band")
        elif not (c.scalp_min_market_cap_sol <= t.market_cap_sol <= c.scalp_max_market_cap_sol):
            a.reasons.append("mcap outside band")
        pump = (t.last_price / w.low - 1) * 100 if w.low > 0 else 0.0
        if pump > c.scalp_max_pump_pct:
            a.reasons.append(f"already +{pump:.0f}% in {c.scalp_window_sec:.0f}s")
        if c.scalp_require_new_high and w.high > 0 and t.last_price < w.high * 0.98:
            a.reasons.append("not at a new high")
        if c.scalp_dip_min_pct > 0:
            hi = t.window(now, c.scalp_dip_window_sec).high
            dip = (1 - t.last_price / hi) * 100 if hi > 0 else 0.0
            if not (c.scalp_dip_min_pct <= dip <= (c.scalp_dip_max_pct or 100)):
                a.reasons.append(f"dip {dip:.0f}% not in {c.scalp_dip_min_pct:.0f}-{c.scalp_dip_max_pct:.0f}%")

        # --- activity gates ----------------------------------------------------
        if w.trades < c.scalp_min_trades:
            a.reasons.append(f"only {w.trades} trades")
        if w.traders < c.scalp_min_unique_traders:
            a.reasons.append(f"only {w.traders} traders")
        if w.trades and w.diversity < c.rug_min_trader_diversity:
            a.reasons.append(f"wash trading? {w.traders} wallets made {w.trades} trades")
        if w.buy_ratio < c.scalp_min_buy_ratio:
            a.reasons.append(f"buy pressure {w.buy_ratio:.0%}")
        if w.net_inflow_sol < c.scalp_min_net_inflow_sol:
            a.reasons.append(f"net inflow {w.net_inflow_sol:+.1f} SOL")

        # --- potential score -------------------------------------------------------
        a.score = 100 * (
            0.30 * _scale(w.trades, 0, 2 * c.scalp_min_trades)
            + 0.20 * _scale(w.traders, 0, 2 * c.scalp_min_unique_traders)
            + 0.20 * _scale(w.buy_ratio, 0.5, 0.9)
            + 0.15 * _scale(w.net_inflow_sol, 0, 3 * c.scalp_min_net_inflow_sol)
            + 0.15 * _scale(w.acceleration, 0.8, 1.5)
        )
        if not a.reasons and a.score < c.scalp_min_score:
            a.reasons.append(f"score {a.score:.0f} < {c.scalp_min_score:.0f}")
        a.approved = not a.reasons
        return a

    def exit_signal(self, t: TokenState, evt_trader: str, evt_tokens: float, evt_is_buy: bool, now: float) -> str:
        """Reasons to dump a held coin immediately, checked on every trade."""
        c = self.cfg
        if not evt_is_buy:
            if t.creator and evt_trader == t.creator:
                return "dev is selling (rug risk)"
            if t.pct(evt_tokens) >= c.rug_dump_exit_pct:
                return f"whale dumped {t.pct(evt_tokens):.1f}% of supply"
        w = t.window(now, c.scalp_fade_window_sec)
        if w.trades >= 5 and w.volume_sol > 0 and w.sell_sol / w.volume_sol >= c.scalp_fade_sell_ratio:
            return f"momentum faded: {w.sell_sol / w.volume_sol:.0%} selling in last {c.scalp_fade_window_sec:.0f}s"
        return ""
