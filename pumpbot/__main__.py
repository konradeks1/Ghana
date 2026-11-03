"""Entry point: python -m pumpbot"""

from __future__ import annotations

import asyncio
import logging

from .bot import CopyTradeBot
from .config import Config
from .executor import make_executor


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    log = logging.getLogger("pumpbot")
    log.info(
        "mode=%s  follow top %d  TP=+%.0f%%  SL=-%.0f%%  size=%.3f SOL",
        "PAPER" if cfg.dry_run else "LIVE", cfg.top_n_traders, cfg.take_profit_pct,
        cfg.stop_loss_pct, cfg.buy_amount_sol,
    )
    bot = CopyTradeBot(cfg, make_executor(cfg))
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log.info("stopped")


if __name__ == "__main__":
    main()
