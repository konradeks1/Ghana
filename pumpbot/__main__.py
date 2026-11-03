"""Entry point: python -m pumpbot"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace

from .bot import CopyTradeBot
from .ensemble import EnsembleBot
from .scalper import ScalperBot
from .config import Config
from .executor import make_executor


def main() -> None:
    cfg = Config.from_env()
    tactic = os.getenv("TACTIC", "").strip()
    if tactic:
        from .tactics import TACTICS

        if tactic not in TACTICS:
            raise SystemExit(f"unknown TACTIC {tactic!r}; choose from: {', '.join(TACTICS)}")
        cfg = replace(cfg, **TACTICS[tactic])
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("pumpbot")
    mode = "PAPER" if cfg.dry_run else "LIVE"
    if cfg.strategy == "ensemble":
        log.info(
            "mode=%s strategy=ensemble  tactics=%s  top %d trade  any-profit=%s (lock +%.1f%%, floor +%.1f%% net)  "
            "SL=-%.0f%%  slippage<=%.0f%%  size=%.3f SOL",
            mode, ",".join(cfg.ensemble_tactics), cfg.ensemble_top_k, cfg.any_profit_mode,
            cfg.any_profit_arm_pct, cfg.any_profit_floor_pct, cfg.stop_loss_pct, cfg.slippage_pct, cfg.buy_amount_sol,
        )
        bot = EnsembleBot(cfg, make_executor(cfg))
    elif cfg.strategy == "copy":
        log.info(
            "mode=%s strategy=copy  follow top %d  TP=+%.0f%%  SL=-%.0f%%  size=%.3f SOL",
            mode, cfg.top_n_traders, cfg.take_profit_pct, cfg.stop_loss_pct, cfg.buy_amount_sol,
        )
        bot = CopyTradeBot(cfg, make_executor(cfg))
    else:
        log.info(
            "mode=%s strategy=scalp tactic=%s  TP=+%.0f%%  SL=-%.0f%%  max hold %ds  size=%.3f SOL  "
            "socials=%s(%s)  min trades=%d/%.0fs",
            mode, tactic or "default", cfg.take_profit_pct, cfg.stop_loss_pct, cfg.scalp_max_hold_sec, cfg.buy_amount_sol,
            cfg.socials_mode, "/".join(cfg.required_socials), cfg.scalp_min_trades, cfg.scalp_window_sec,
        )
        bot = ScalperBot(cfg, make_executor(cfg))
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
