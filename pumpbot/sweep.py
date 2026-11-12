"""Strategy search: try many variants of each bot on recorded data, pick the best,
then check the pick on a recording it was NOT chosen on.

    python -m pumpbot.sweep --train rec1.jsonl,rec2.jsonl --test rec3.jsonl --per-family 40

Each family starts from one bot's tactic and varies its entry and exit rules.
Variants are scored on the training recordings (total profit, with at least
--min-trades trades); the best per family is then replayed on the test
recording to see whether the edge holds on unseen data.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import os
import random
from concurrent.futures import ProcessPoolExecutor

from .tactics import TACTICS

# search space per family: setting -> values to try
SPACE: dict[str, dict] = {
    "bot1_sniper": dict(
        scalp_min_age_sec=[3, 10, 20], scalp_max_age_sec=[90, 180, 300],
        scalp_min_trades=[6, 10, 15], scalp_min_buy_ratio=[0.55, 0.65],
        stop_loss_pct=[10, 15, 20], scalp_max_hold_sec=[90, 180, 300],
        scalp_fade_sell_ratio=[0.85, 1.01], any_profit_arm_pct=[3, 8, 15],
        trailing_stop_pct=[8, 15],
    ),
    "bot2_breakout": dict(
        scalp_require_new_high=[True, False], scalp_max_pump_pct=[8, 25, 60],
        scalp_min_trades=[15, 25, 40], scalp_min_buy_ratio=[0.55, 0.65],
        stop_loss_pct=[6, 10, 15], scalp_max_hold_sec=[120, 300, 600],
        scalp_fade_sell_ratio=[0.75, 0.9, 1.01], any_profit_arm_pct=[3, 8],
        trailing_stop_pct=[5, 10],
    ),
    "bot3_volume": dict(
        scalp_window_sec=[30, 60], scalp_min_trades=[15, 20, 30],
        scalp_min_unique_traders=[8, 12], scalp_min_buy_ratio=[0.5, 0.6],
        scalp_max_pump_pct=[20, 60, 150], stop_loss_pct=[8, 12, 18],
        scalp_max_hold_sec=[60, 120, 300], scalp_fade_sell_ratio=[0.8, 1.01],
        any_profit_arm_pct=[3, 8],
    ),
}


def variants(family: str, n: int, seed: int = 7) -> dict[str, dict]:
    space = SPACE[family]
    keys = list(space)
    combos = list(itertools.product(*(space[k] for k in keys)))
    random.Random(seed).shuffle(combos)
    out = {f"{family}#v0": dict(TACTICS[family])}  # the current version is always in the race
    for i, combo in enumerate(combos[: n - 1], 1):
        out[f"{family}#v{i}"] = {**TACTICS[family], **dict(zip(keys, combo))}
    return out


def _replay_one(args: tuple[str, list[str], str, str]) -> dict:
    path, names, out_dir, extra_path = args
    logging.disable(logging.CRITICAL)
    from .experiment import replay

    with open(extra_path) as f:
        extra = json.load(f)
    results = asyncio.run(replay(path, names, out_dir, extra))
    return {r["tactic"]: r for r in results}


def run_all(recordings: list[str], tactics: dict[str, dict], out_dir: str, workers: int) -> dict[str, dict[str, dict]]:
    os.makedirs(out_dir, exist_ok=True)
    extra_path = os.path.join(out_dir, "variants.json")
    with open(extra_path, "w") as f:
        json.dump(tactics, f)
    names = list(tactics)
    # split tactics into chunks so every core is busy
    chunk = max(1, len(names) // max(1, workers // max(1, len(recordings))) + 1)
    jobs = []
    for rec in recordings:
        for i in range(0, len(names), chunk):
            sub = os.path.join(out_dir, f"{os.path.basename(rec)}.{i}")
            jobs.append((rec, names[i:i + chunk], sub, extra_path))
    per_rec: dict[str, dict[str, dict]] = {rec: {} for rec in recordings}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for (rec, *_), res in zip(jobs, pool.map(_replay_one, jobs)):
            per_rec[rec].update(res)
    return per_rec


def score(per_rec: dict[str, dict[str, dict]], name: str) -> tuple[float, int, int, list[float]]:
    pnls = [per_rec[r][name]["pnl_usd"] for r in per_rec]
    trades = sum(per_rec[r][name]["trades"] for r in per_rec)
    wins = sum(per_rec[r][name]["wins"] for r in per_rec)
    return sum(pnls), trades, wins, pnls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--test", required=True)
    ap.add_argument("--families", default=",".join(SPACE))
    ap.add_argument("--per-family", type=int, default=40)
    ap.add_argument("--min-trades", type=int, default=5)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--out", default="sweep")
    args = ap.parse_args()
    train = [p for p in args.train.split(",") if p]
    test = [p for p in args.test.split(",") if p]
    fams = [f for f in args.families.split(",") if f]

    tactics: dict[str, dict] = {}
    for fam in fams:
        tactics.update(variants(fam, args.per_family))
    print(f"testing {len(tactics)} variants on {len(train)} training recordings ...", flush=True)
    tr = run_all(train, tactics, os.path.join(args.out, "train"), args.workers)

    picks: dict[str, str] = {}
    report: dict[str, dict] = {"train": {}, "test": {}, "picks": {}}
    for fam in fams:
        ranked = sorted(
            (n for n in tactics if n.startswith(fam + "#")),
            key=lambda n: score(tr, n)[0], reverse=True,
        )
        eligible = [n for n in ranked if score(tr, n)[1] >= args.min_trades] or ranked
        picks[fam] = eligible[0]
        print(f"\n{fam}: top 5 on training data ($ profit per $100 bot, trades, wins, per recording)")
        for n in eligible[:5]:
            p, t, w, per = score(tr, n)
            print(f"  {n:<20} {p:+8.2f} $  {t:>4} trades  {w:>3} wins  {[round(x, 2) for x in per]}")
        cur = f"{fam}#v0"
        p, t, w, per = score(tr, cur)
        print(f"  {'(current) ' + cur:<20} {p:+8.2f} $  {t:>4} trades  {w:>3} wins")
        report["train"][fam] = {n: score(tr, n) for n in eligible[:10]}

    print(f"\nchecking picks on {len(test)} unseen recording(s) ...", flush=True)
    chosen = {picks[f]: tactics[picks[f]] for f in fams} | {f"{f}#v0": tactics[f"{f}#v0"] for f in fams}
    te = run_all(test, chosen, os.path.join(args.out, "test"), args.workers)
    for fam in fams:
        for n in (picks[fam], f"{fam}#v0"):
            p, t, w, per = score(te, n)
            tag = "PICK   " if n == picks[fam] else "current"
            print(f"  {tag} {n:<20} unseen data: {p:+8.2f} $  {t:>3} trades  {w:>3} wins")
            report["test"][n] = (p, t, w)
        diff = {k: v for k, v in tactics[picks[fam]].items() if TACTICS[fam].get(k) != v}
        report["picks"][fam] = {"name": picks[fam], "changes": diff}
        print(f"  {fam} changes: {diff}")
    with open(os.path.join(args.out, "report.json"), "w") as f:
        json.dump(report, f, indent=1, default=str)


if __name__ == "__main__":
    main()
