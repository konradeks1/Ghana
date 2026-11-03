"""Run several scalp tactics side by side on the same live data (paper only).

    python -m pumpbot.experiment --minutes 10 --tactics baseline,quick_flip,early_bird

Every tactic gets its own simulated wallet and positions but sees exactly the
same coins and trades over the same minutes, so the comparison is fair. Social
link and rug checks stay on for every tactic. At the end the runner prints a
league table and writes results to --out.
"""

from __future__ import annotations

import argparse
import asyncio
import heapq
import itertools
import json
import logging
import os
import statistics
import time
from collections import Counter
from dataclasses import replace

from .config import Config
from .executor import PaperExecutor
from .feed import PumpPortalFeed
from .scalper import ScalperBot
from .socials import SocialsChecker

# Each tactic is a set of overrides on top of the .env / default config.
TACTICS: dict[str, dict] = {
    # current defaults: strict activity gates, +20% runner, 3 min max hold
    "baseline": {},
    # many small, fast wins: looser gates, +10% target, tight stop, short hold
    "quick_flip": dict(
        scalp_min_trades=15, scalp_min_unique_traders=10, scalp_min_buy_ratio=0.55,
        scalp_min_net_inflow_sol=1.0, scalp_min_score=45, scalp_min_market_cap_sol=20,
        scalp_max_market_cap_sol=600, take_profit_pct=10, tp_floor_pct=10, trailing_stop_pct=5,
        stop_loss_pct=8, scalp_max_hold_sec=90, scalp_fade_sell_ratio=0.75,
    ),
    # get in before the crowd: very young coins, short window, bigger target
    "early_bird": dict(
        scalp_min_age_sec=5, scalp_max_age_sec=120, scalp_window_sec=20, scalp_min_trades=10,
        scalp_min_unique_traders=8, scalp_min_net_inflow_sol=1.0, scalp_min_score=45,
        scalp_min_market_cap_sol=25, take_profit_pct=25, tp_floor_pct=25, stop_loss_pct=15,
        scalp_max_hold_sec=120, scalp_fade_sell_ratio=0.8,
    ),
    # ride breakouts: only buy at a fresh high with accelerating trading
    "breakout": dict(
        scalp_require_new_high=True, scalp_min_trades=25, scalp_min_unique_traders=12,
        scalp_max_pump_pct=300, scalp_min_score=55, take_profit_pct=15, tp_floor_pct=15,
        trailing_stop_pct=7, stop_loss_pct=10, scalp_max_hold_sec=150, scalp_fade_sell_ratio=0.75,
    ),
    # buy the dip: busy coin that pulled back 15-40% from its 3 min high
    "dip_buyer": dict(
        scalp_dip_min_pct=15, scalp_dip_max_pct=40, scalp_min_trades=20, scalp_min_unique_traders=10,
        scalp_min_buy_ratio=0.50, scalp_min_net_inflow_sol=0.0, scalp_min_score=40,
        take_profit_pct=15, tp_floor_pct=15, trailing_stop_pct=6, stop_loss_pct=12,
        scalp_max_hold_sec=180, scalp_fade_sell_ratio=0.8,
    ),
    # bigger, more established coins near the top of the bonding curve
    "king_of_hill": dict(
        scalp_min_market_cap_sol=150, scalp_max_market_cap_sol=1000, scalp_max_age_sec=3600,
        scalp_min_trades=40, scalp_min_unique_traders=20, scalp_min_net_inflow_sol=3.0,
        take_profit_pct=12, tp_floor_pct=12, trailing_stop_pct=6, stop_loss_pct=8,
        scalp_max_hold_sec=240, scalp_fade_sell_ratio=0.75,
    ),
    # let the momentum-fade exit breathe: same entries as baseline, exits only on TP/SL/time
    "diamond_hands": dict(
        scalp_fade_sell_ratio=1.01, stop_loss_pct=15, scalp_max_hold_sec=300,
        trailing_stop_pct=12,
    ),
    # ---- round 2: looser gates (round 1 found almost nothing to buy) ----
    "quick_flip_loose": dict(
        scalp_window_sec=30, scalp_min_trades=8, scalp_min_unique_traders=6, scalp_min_buy_ratio=0.55,
        scalp_min_net_inflow_sol=0.5, scalp_min_score=35, scalp_min_market_cap_sol=15,
        scalp_max_market_cap_sol=800, take_profit_pct=8, tp_floor_pct=8, trailing_stop_pct=4,
        stop_loss_pct=7, scalp_max_hold_sec=60, scalp_fade_sell_ratio=0.8,
    ),
    "early_bird_loose": dict(
        scalp_min_age_sec=3, scalp_max_age_sec=90, scalp_window_sec=15, scalp_min_trades=6,
        scalp_min_unique_traders=5, scalp_min_net_inflow_sol=0.3, scalp_min_score=30,
        scalp_min_market_cap_sol=15, take_profit_pct=20, tp_floor_pct=20, stop_loss_pct=12,
        scalp_max_hold_sec=90, scalp_fade_sell_ratio=0.85,
    ),
    "dip_buyer_loose": dict(
        scalp_dip_min_pct=10, scalp_dip_max_pct=45, scalp_min_trades=10, scalp_min_unique_traders=6,
        scalp_min_buy_ratio=0.45, scalp_min_net_inflow_sol=-1.0, scalp_min_score=30,
        scalp_min_market_cap_sol=15, scalp_max_market_cap_sol=800,
        take_profit_pct=12, tp_floor_pct=12, trailing_stop_pct=5, stop_loss_pct=10,
        scalp_max_hold_sec=180, scalp_fade_sell_ratio=0.85,
    ),
    # dip entries but let winners run longer
    "dip_runner": dict(
        scalp_dip_min_pct=10, scalp_dip_max_pct=45, scalp_min_trades=10, scalp_min_unique_traders=6,
        scalp_min_buy_ratio=0.45, scalp_min_net_inflow_sol=-1.0, scalp_min_score=30,
        scalp_min_market_cap_sol=15, scalp_max_market_cap_sol=800,
        take_profit_pct=20, tp_floor_pct=15, trailing_stop_pct=10, stop_loss_pct=12,
        scalp_max_hold_sec=300, scalp_fade_sell_ratio=1.01,
    ),
    # baseline logic with half the activity requirement
    "momentum_lite": dict(
        scalp_min_trades=15, scalp_min_unique_traders=10, scalp_min_net_inflow_sol=1.0,
        scalp_min_score=50, scalp_min_market_cap_sol=20, scalp_max_market_cap_sol=600,
        take_profit_pct=15, tp_floor_pct=15, trailing_stop_pct=7, stop_loss_pct=10,
    ),
    # pure trade-count hunter: buy wherever trading is busiest, take +10% fast
    "volume_hunter": dict(
        scalp_window_sec=30, scalp_min_trades=20, scalp_min_unique_traders=10, scalp_min_buy_ratio=0.5,
        scalp_min_net_inflow_sol=0.0, scalp_min_score=0, scalp_min_market_cap_sol=15,
        scalp_max_market_cap_sol=1000, take_profit_pct=10, tp_floor_pct=10, trailing_stop_pct=5,
        stop_loss_pct=8, scalp_max_hold_sec=120, scalp_fade_sell_ratio=0.8,
    ),
}


class FeedProxy:
    """Per-tactic view of the shared feed; ref-counts token subscriptions."""

    def __init__(self, mux: "FeedMux"):
        self.mux = mux
        self.tokens: set[str] = set()

    async def subscribe_tokens(self, mints):
        new = set(mints) - self.tokens
        self.tokens |= new
        await self.mux.feed.subscribe_tokens(new)

    async def unsubscribe_tokens(self, mints):
        gone = set(mints) & self.tokens
        self.tokens -= gone
        still = set().union(*(p.tokens for p in self.mux.proxies))
        await self.mux.feed.unsubscribe_tokens(gone - still)

    async def set_accounts(self, wallets):
        pass


class NullFeed:
    def __init__(self):
        self.tokens: set[str] = set()

    async def subscribe_tokens(self, m):
        pass

    async def unsubscribe_tokens(self, m):
        pass


class FeedMux:
    def __init__(self, url: str | None, record_path: str | None = None):
        self.bots: list[ScalperBot] = []
        self.proxies: list[FeedProxy] = []
        self.feed = PumpPortalFeed(url, self.handle) if url else NullFeed()
        self.messages = 0
        self._rec = open(record_path, "a") if record_path else None

    def proxy(self) -> FeedProxy:
        p = FeedProxy(self)
        self.proxies.append(p)
        return p

    async def handle(self, msg: dict) -> None:
        self.messages += 1
        if self._rec:
            self._rec.write(json.dumps({"t": time.time(), "m": msg}) + "\n")
        for b in self.bots:
            try:
                await b.handle(msg)
            except Exception:
                b.log.exception("handler error")


class VirtualClock:
    """Replay time: sleeps resolve when the replay reaches their wake-up time."""

    def __init__(self, start: float):
        self.now = start
        self._waiters: list = []
        self._seq = itertools.count()

    def time(self) -> float:
        return self.now

    async def sleep(self, dt: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self.now + dt, next(self._seq), fut))
        await fut

    async def advance(self, t: float) -> None:
        while self._waiters and self._waiters[0][0] <= t:
            wake, _, fut = heapq.heappop(self._waiters)
            self.now = max(self.now, wake)
            if not fut.done():
                fut.set_result(None)
            for _ in range(10):  # let woken orders finish
                await asyncio.sleep(0)
        self.now = max(self.now, t)
        await asyncio.sleep(0)


class RecordedSocials(SocialsChecker):
    def __init__(self, meta: dict[str, dict | None]):
        super().__init__([])
        self.meta = meta

    async def fetch_metadata(self, uri: str):
        return self.meta.get(uri)


def dump_metadata(socials: SocialsChecker, path: str) -> None:
    meta = {}
    for uri, fut in socials._cache.items():
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            meta[uri] = fut.result()
    existing = {}
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    existing.update(meta)
    with open(path, "w") as f:
        json.dump(existing, f)


def summarize(name: str, bot: ScalperBot, overrides: dict) -> dict:
    closed = bot.positions.closed
    pnls = [t.pnl_sol for t in closed]
    pcts = [t.pnl_sol / t.sol_in * 100 for t in closed if t.sol_in]
    reasons = Counter(t.reason.split(" ")[0] if not t.reason.startswith("runner") else "runner-" + t.reason.split(" ")[1]
                      for t in closed)
    return {
        "tactic": name,
        "trades": len(closed),
        "open_at_end": len(bot.positions.open),
        "wins": sum(1 for p in pnls if p > 0),
        "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else 0.0,
        "pnl_sol": sum(pnls),
        "avg_pct": statistics.mean(pcts) if pcts else 0.0,
        "best_pct": max(pcts) if pcts else 0.0,
        "worst_pct": min(pcts) if pcts else 0.0,
        "exit_reasons": dict(reasons),
        "coins_seen": bot.counts["coins"],
        "with_socials": bot.counts["with_socials"],
        "rug_flags": bot.counts["rug_flags"],
        "overrides": overrides,
        "trade_log": [
            {"mint": t.mint, "pnl_sol": round(t.pnl_sol, 5), "pct": round(t.pnl_sol / t.sol_in * 100, 2),
             "held_s": round(t.closed_at - t.opened_at, 1), "reason": t.reason}
            for t in closed
        ],
    }


def table(rows: list[dict]) -> str:
    rows = sorted(rows, key=lambda r: r["pnl_sol"], reverse=True)
    out = [f"{'tactic':<14}{'trades':>7}{'win%':>7}{'pnl SOL':>10}{'avg %':>8}{'best %':>8}{'worst %':>9}  exits"]
    for r in rows:
        exits = ", ".join(f"{k}×{v}" for k, v in sorted(r["exit_reasons"].items(), key=lambda kv: -kv[1]))
        out.append(
            f"{r['tactic']:<14}{r['trades']:>7}{r['win_rate'] * 100:>6.0f}%{r['pnl_sol']:>+10.4f}"
            f"{r['avg_pct']:>+8.1f}{r['best_pct']:>+8.1f}{r['worst_pct']:>+9.1f}  {exits}"
        )
    return "\n".join(out)


def _build(mux, names, tactics, base, out_dir, socials, clock=time.time, sleep=asyncio.sleep):
    for name in names:
        cfg = replace(base, **tactics[name], state_file=os.path.join(out_dir, f"{name}.state.json"))
        bot = ScalperBot(cfg, PaperExecutor(cfg, sleep=sleep), feed=mux.proxy(), clock=clock, socials=socials)
        bot.log = logging.getLogger(f"pumpbot.{name}")
        mux.bots.append(bot)


def _base_config() -> Config:
    base = Config.from_env()
    return replace(base, dry_run=True, max_daily_loss_sol=1e9)  # paper only; let every tactic trade


async def replay(path: str, names: list[str], out_dir: str, extra: dict[str, dict] | None = None) -> list[dict]:
    """Re-run tactics on a recorded stream, at full speed, with the same latency rules."""
    base = _base_config()
    tactics = {**TACTICS, **(extra or {})}
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.endswith(".state.json"):
            os.remove(os.path.join(out_dir, f))
    with open(path + ".meta.json") as f:
        meta = json.load(f)
    with open(path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    events.sort(key=lambda e: e["t"])
    clock = VirtualClock(events[0]["t"])
    mux = FeedMux(None)
    _build(mux, names, tactics, base, out_dir, RecordedSocials(meta), clock=clock.time, sleep=clock.sleep)
    next_tick = events[0]["t"] + 1
    for e in events:
        while next_tick <= e["t"]:
            await clock.advance(next_tick)
            for b in mux.bots:
                await b.tick()
            next_tick += 1
        await clock.advance(e["t"])
        await mux.handle(e["m"])
    await clock.advance(events[-1]["t"] + 5)
    for b in mux.bots:
        for mint in list(b.positions.open):
            await b._exit(mint, "end of test")
    while any(b._tasks for b in mux.bots):
        await clock.advance(clock.now + 1)
    minutes = (events[-1]["t"] - events[0]["t"]) / 60
    results = [summarize(n, b, tactics[n]) for n, b in zip(names, mux.bots)]
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump({"replay": path, "minutes": minutes, "messages": len(events), "results": results}, f, indent=1)
    logging.getLogger("pumpbot.experiment").info(
        "REPLAY of %.0f min (%d msgs):\n%s", minutes, len(events), table(results)
    )
    return results


async def run(
    minutes: float, names: list[str], out_dir: str, extra: dict[str, dict] | None = None, record: str | None = None
) -> list[dict]:
    base = _base_config()
    tactics = {**TACTICS, **(extra or {})}
    os.makedirs(out_dir, exist_ok=True)
    mux = FeedMux(base.ws_endpoint, record)
    socials = SocialsChecker(base.ipfs_gateways)
    _build(mux, names, tactics, base, out_dir, socials)
    log = logging.getLogger("pumpbot.experiment")
    log.info("running %d tactics for %.0f min: %s", len(names), minutes, ", ".join(names))

    feed_task = asyncio.create_task(mux.feed.run())
    start = last_report = time.time()
    try:
        while time.time() - start < minutes * 60 and not feed_task.done():
            await asyncio.sleep(1)
            for b in mux.bots:
                await b.tick()
            if time.time() - last_report >= 60:
                last_report = time.time()
                rows = [summarize(n, b, {}) for n, b in zip(names, mux.bots)]
                log.info("after %.0f min (%d msgs):\n%s", (time.time() - start) / 60, mux.messages, table(rows))
        # close anything still open at the last seen price so results are complete
        for b in mux.bots:
            for mint in list(b.positions.open):
                await b._exit(mint, "end of test")
        for b in mux.bots:
            await b.drain()
    finally:
        mux.feed.stop()
        feed_task.cancel()
        if record:
            mux._rec.close()
            dump_metadata(socials, record + ".meta.json")
        await socials.close()

    results = [summarize(n, b, tactics[n]) for n, b in zip(names, mux.bots)]
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump({"minutes": minutes, "messages": mux.messages, "results": results}, f, indent=1)
    log.info("FINAL after %.0f min:\n%s", minutes, table(results))
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--tactics", default=",".join(TACTICS))
    ap.add_argument("--extra", default="", help="JSON file with additional {name: overrides}")
    ap.add_argument("--out", default="experiments")
    ap.add_argument("--record", default="", help="append the live stream to this .jsonl file")
    ap.add_argument("--replay", default="", help="replay a recorded .jsonl instead of going live")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-22s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    extra = {}
    if args.extra:
        with open(args.extra) as f:
            extra = json.load(f)
    tactics = {**TACTICS, **extra}
    names = list(tactics) if args.tactics == "all" else [n.strip() for n in args.tactics.split(",") if n.strip()]
    if args.replay:
        asyncio.run(replay(args.replay, names, args.out, extra))
    else:
        asyncio.run(run(args.minutes, names, args.out, extra, args.record or None))


if __name__ == "__main__":
    main()
