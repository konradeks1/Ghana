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
from .market import MarketData
from .mux import FeedMux
from .scalper import ScalperBot
from .socials import SocialsChecker
from .tactics import TACTICS



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
        "coins_seen": getattr(bot, "counts", Counter())["coins"],
        "with_socials": getattr(bot, "counts", Counter())["with_socials"],
        "rug_flags": getattr(bot, "counts", Counter())["rug_flags"],
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


def _build(mux, names, tactics, base, out_dir, socials, clock=time.time, sleep=asyncio.sleep, market=None):
    for name in names:
        cfg = replace(base, **tactics[name], state_file=os.path.join(out_dir, f"{name}.state.json"))
        bot = ScalperBot(cfg, PaperExecutor(cfg, sleep=sleep), feed=mux.proxy(), clock=clock, socials=socials,
                         market=market)
        bot.log = logging.getLogger(f"pumpbot.{name}")
        mux.bots.append(bot)


def _add_ensemble(mux, base, out_dir, clock=time.time, sleep=asyncio.sleep, socials=None, extra=None, market=None):
    from .ensemble import EnsembleBot

    edir = os.path.join(out_dir, "ensemble")
    os.makedirs(edir, exist_ok=True)
    for f in os.listdir(edir):
        os.remove(os.path.join(edir, f))
    cfg = replace(base, ensemble_state_dir=edir, state_file=os.path.join(out_dir, "ENSEMBLE.state.json"))
    bot = EnsembleBot(cfg, PaperExecutor(cfg, sleep=sleep), feed=mux.proxy(), clock=clock, sleep=sleep,
                      socials=socials, tactics={**TACTICS, **(extra or {})}, market=market)
    bot.log = logging.getLogger("pumpbot.ENSEMBLE")
    mux.bots.append(bot)
    return bot


def _base_config() -> Config:
    base = Config.from_env()
    return replace(base, dry_run=True, max_daily_loss_sol=1e9)  # paper only; let every tactic trade


async def replay(
    path: str, names: list[str], out_dir: str, extra: dict[str, dict] | None = None, ensemble: bool = False
) -> list[dict]:
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
    socials = RecordedSocials(meta)
    market = MarketData(base, offline=True)
    _build(mux, names, tactics, base, out_dir, socials, clock=clock.time, sleep=clock.sleep, market=market)
    if ensemble:
        _add_ensemble(mux, base, out_dir, clock.time, clock.sleep, socials, extra, market)
        names = names + ["ENSEMBLE"]
        tactics = {**tactics, "ENSEMBLE": {"strategy": "ensemble"}}
    next_tick = events[0]["t"] + 1
    for e in events:
        while next_tick <= e["t"]:
            await clock.advance(next_tick)
            for b in mux.bots:
                await b.tick()
            next_tick += 1
        await clock.advance(e["t"])
        if "x" in e:
            market.apply_record(e["x"], e["t"])
            continue
        await mux.handle(e["m"])
    await clock.advance(events[-1]["t"] + 5)
    for b in mux.bots:
        for mint in list(b.positions.open):
            await b._exit(mint, "end of test")
    while any(b._tasks for b in mux.bots) or any(
        s._tasks for b in mux.bots for s in getattr(b, "shadows", {}).values()
    ):
        await clock.advance(clock.now + 1)
    for b in mux.bots:
        b.save_state()
    minutes = (events[-1]["t"] - events[0]["t"]) / 60
    results = [summarize(n, b, tactics[n]) for n, b in zip(names, mux.bots)]
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump({"replay": path, "minutes": minutes, "messages": len(events), "results": results}, f, indent=1)
    logging.getLogger("pumpbot.experiment").info(
        "REPLAY of %.0f min (%d msgs):\n%s", minutes, len(events), table(results)
    )
    return results


async def run(
    minutes: float, names: list[str], out_dir: str, extra: dict[str, dict] | None = None, record: str | None = None,
    ensemble: bool = False,
) -> list[dict]:
    base = _base_config()
    tactics = {**TACTICS, **(extra or {})}
    os.makedirs(out_dir, exist_ok=True)
    mux = FeedMux(base.ws_endpoint, record)
    socials = SocialsChecker(base.ipfs_gateways)
    market = MarketData(base, recorder=mux.record_extra if record else None)
    _build(mux, names, tactics, base, out_dir, socials, market=market)
    if ensemble:
        _add_ensemble(mux, base, out_dir, socials=socials, extra=extra, market=market)
        names = names + ["ENSEMBLE"]
        tactics = {**tactics, "ENSEMBLE": {"strategy": "ensemble"}}
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
                rows = [summarize(n, b, tactics.get(n, {})) for n, b in zip(names, mux.bots)]
                log.info("after %.0f min (%d msgs):\n%s", (time.time() - start) / 60, mux.messages, table(rows))
                with open(os.path.join(out_dir, "results.json"), "w") as f:  # interim, survives a crash
                    json.dump({"minutes": (time.time() - start) / 60, "messages": mux.messages,
                               "interim": True, "results": rows}, f, indent=1)
                for b in mux.bots:
                    b.save_state()
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
        await market.close()

    results = [summarize(n, b, tactics[n]) for n, b in zip(names, mux.bots)]
    with open(os.path.join(out_dir, "results.json"), "w") as f:
        json.dump({"minutes": minutes, "messages": mux.messages, "results": results}, f, indent=1)
    log.info("FINAL after %.0f min:\n%s", minutes, table(results))
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--tactics", default="all", help="names, or groups: all / est / new")
    ap.add_argument("--extra", default="", help="JSON file with additional {name: overrides}")
    ap.add_argument("--out", default="experiments")
    ap.add_argument("--record", default="", help="append the live stream to this .jsonl file")
    ap.add_argument("--replay", default="", help="replay a recorded .jsonl instead of going live")
    ap.add_argument("--ensemble", action="store_true", help="also run the ensemble (all tactics, best ones trade)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-22s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    extra = {}
    if args.extra:
        with open(args.extra) as f:
            extra = json.load(f)
    tactics = {**TACTICS, **extra}
    from .ensemble import resolve_tactics

    names = resolve_tactics([n.strip() for n in args.tactics.split(",") if n.strip()], tactics)
    if args.replay:
        asyncio.run(replay(args.replay, names, args.out, extra, args.ensemble))
    else:
        asyncio.run(run(args.minutes, names, args.out, extra, args.record or None, args.ensemble))


if __name__ == "__main__":
    main()
