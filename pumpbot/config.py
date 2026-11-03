"""Runtime configuration, loaded from environment variables / a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _list(name: str, default: str) -> list[str]:
    return [x.strip().lower() for x in _env(name, default).split(",") if x.strip()]


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # --- mode -------------------------------------------------------------
    strategy: str = "ensemble"  # "ensemble" (all tactics, best ones trade), "scalp" or "copy"
    dry_run: bool = True  # paper trading unless explicitly disabled
    paper_balance_sol: float = 5.0

    # --- endpoints / wallet ----------------------------------------------
    ws_url: str = "wss://pumpportal.fun/api/data"
    trade_url: str = "https://pumpportal.fun/api/trade-local"
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    private_key: str = ""  # base58 secret key, only needed when dry_run=False
    api_key: str = ""  # PumpPortal API key: required for trade/account streams

    # --- trader discovery ---------------------------------------------------
    top_n_traders: int = 25
    leaderboard_window_hours: float = 24.0
    min_round_trips: int = 3  # a wallet needs this many closed trades to rank
    min_median_hold_sec: float = 120.0  # skip scalpers that flip faster than we can copy
    leaderboard_refresh_sec: int = 300
    max_tracked_tokens: int = 400  # how many fresh tokens we scan for traders
    seed_traders_file: str = "traders.txt"  # optional bootstrap wallets
    exclude_wallets: set[str] = field(default_factory=set)

    # --- strategy -----------------------------------------------------------
    take_profit_pct: float = 20.0  # the +20% target
    stop_loss_pct: float = 12.0
    max_hold_sec: int = 1200
    exit_on_leader_sell: bool = True
    # winners: +TP is the minimum, not the cap
    let_winners_run: bool = True
    trailing_stop_pct: float = 10.0  # sell when price drops this far from its peak
    tp_floor_pct: float = 20.0  # ...but never exit a runner below this gain
    runner_max_hold_sec: int = 3600
    # any-profit rule: bank ANY net gain instead of waiting for +20%
    any_profit_mode: bool = True
    any_profit_arm_pct: float = 3.0  # net gain (after estimated exit costs) that locks the trade in
    any_profit_floor_pct: float = 1.0  # a locked trade never sells below this net gain
    exit_cost_pct: float = 1.5  # estimated sell fee + slippage, used to compute net gain

    # --- entry analysis -----------------------------------------------------
    min_win_prob: float = 0.55
    min_expected_value_pct: float = 3.0
    max_chase_pct: float = 8.0  # skip if price already ran this far past leader's entry
    confluence_window_sec: int = 300
    confluence_bonus: float = 0.15
    min_leader_buy_sol: float = 0.3
    min_market_cap_sol: float = 25.0
    max_market_cap_sol: float = 400.0
    outcome_horizon_sec: int = 1800

    # --- execution / risk ---------------------------------------------------
    buy_amount_sol: float = 0.1
    max_open_positions: int = 3
    max_daily_loss_sol: float = 0.5
    mint_cooldown_sec: int = 1800
    slippage_pct: float = 5.0  # max price move tolerated on a fill (was 10)
    priority_fee_sol: float = 0.0005
    fee_pct: float = 1.0  # pump.fun fee per side
    pool: str = "auto"
    confirm_timeout_sec: float = 45.0  # live: wait this long for on-chain confirmation

    # --- paper realism ------------------------------------------------------
    paper_latency_sec: float = 1.5  # delay between decision and simulated fill
    paper_slippage_pct: float = 0.5  # extra slippage on top of the bonding-curve price impact

    # --- scalp strategy: what makes a coin worth buying --------------------
    scalp_min_age_sec: float = 20.0  # let launch bundles/snipes settle first
    scalp_max_age_sec: float = 1800.0  # only trade coins younger than this
    scalp_window_sec: float = 60.0  # activity is measured over this window
    scalp_min_trades: int = 30  # "big amount of trades" in the window
    scalp_min_unique_traders: int = 15
    scalp_min_buy_ratio: float = 0.60  # share of SOL volume that is buying
    scalp_min_net_inflow_sol: float = 2.0
    scalp_max_pump_pct: float = 150.0  # skip if already up this much vs window low
    scalp_min_score: float = 60.0  # 0-100 potential score needed to buy
    scalp_min_market_cap_sol: float = 30.0
    scalp_max_market_cap_sol: float = 350.0
    # socials: must have at least one ("any") or all ("all") of these
    required_socials: list[str] = field(default_factory=lambda: ["website", "twitter", "instagram"])
    socials_mode: str = "any"
    ipfs_gateways: list[str] = field(
        default_factory=lambda: [
            "https://ipfs.filebase.io/ipfs/{cid}",
            "https://pump.mypinata.cloud/ipfs/{cid}",
            "https://4everland.io/ipfs/{cid}",
            "https://ipfs.io/ipfs/{cid}",
        ]
    )
    # rug-pull protection
    rug_max_dev_pct: float = 10.0  # creator's share of supply
    rug_max_top10_pct: float = 35.0  # top-10 wallets' share of supply
    rug_max_single_holder_pct: float = 8.0
    rug_max_early_supply_pct: float = 25.0  # bought in the first 3s (bundled launch)
    rug_min_trader_diversity: float = 0.25  # unique traders / trades (wash trading)
    rug_max_creator_launches_per_hour: int = 3  # serial launchers
    rug_dump_exit_pct: float = 3.0  # exit if one wallet sells this % of supply at once
    # scalp exits / pacing
    scalp_max_hold_sec: int = 180
    scalp_fade_window_sec: float = 15.0
    scalp_fade_sell_ratio: float = 0.65  # exit if sells are >= this share of recent volume
    scalp_reentry_cooldown_sec: int = 60
    scalp_max_entries_per_token: int = 3
    scalp_max_open_positions: int = 5
    scalp_eval_interval_sec: float = 2.0  # re-score each coin at most this often
    # entry styles (off by default)
    scalp_require_new_high: bool = False  # breakout: buy only at/near the window high
    scalp_dip_min_pct: float = 0.0  # dip-buy: require a pullback of at least this % ...
    scalp_dip_max_pct: float = 0.0  # ... but no more than this % from the recent high
    scalp_dip_window_sec: float = 180.0

    # --- established coins ($25k+) -----------------------------------------
    scalp_min_market_cap_usd: float = 0.0  # when set, replaces the SOL market-cap band
    scalp_max_market_cap_usd: float = 0.0
    sol_price_usd: float = 90.0  # fallback until the live SOL price is fetched
    discovery_enabled: bool = False  # find established coins on DexScreener
    discovery_interval_sec: float = 60.0
    discovery_min_mcap_usd: float = 25_000.0
    discovery_max_mcap_usd: float = 10_000_000.0
    discovery_min_h1_volume_usd: float = 10_000.0
    discovery_min_m5_txns: int = 20
    discovery_min_liquidity_usd: float = 8_000.0
    discovery_max_coins: int = 60
    require_holder_check: bool = True  # coins found mid-life need an RPC holder check to be bought
    holder_check_ttl_sec: float = 300.0
    established_idle_drop_sec: float = 900.0  # stop watching an established coin after 15 min without trades

    # --- ensemble: all tactics run in the background, the best ones trade ---
    ensemble_tactics: list[str] = field(default_factory=lambda: ["est"])  # "est" = established-coin tactics
    ensemble_top_k: int = 4  # how many tactics may trade at once
    ensemble_window_min: float = 120.0  # rank on the last 2 hours of simulated trades
    ensemble_explore_bonus: float = 10.0  # % bonus that shrinks as a tactic proves itself
    ensemble_min_score: float = 0.0  # tactics scoring at or below this don't trade
    ensemble_max_open_positions: int = 6
    ensemble_safety_max_hold_sec: int = 900
    ensemble_state_dir: str = "ensemble_state"

    state_file: str = "state.json"
    log_level: str = "INFO"

    @property
    def ws_endpoint(self) -> str:
        if not self.api_key:
            return self.ws_url
        sep = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{sep}api-key={self.api_key}"

    @property
    def round_trip_cost_pct(self) -> float:
        return 2 * self.fee_pct + 2 * self.priority_fee_sol / max(self.buy_amount_sol, 1e-9) * 100

    @classmethod
    def from_env(cls) -> "Config":
        excl = {w.strip() for w in _env("EXCLUDE_WALLETS", "").split(",") if w.strip()}
        d = cls()
        return cls(
            strategy=_env("STRATEGY", "ensemble").lower(),
            any_profit_mode=_bool("ANY_PROFIT_MODE", True),
            any_profit_arm_pct=_float("ANY_PROFIT_ARM_PCT", 3.0),
            any_profit_floor_pct=_float("ANY_PROFIT_FLOOR_PCT", 1.0),
            exit_cost_pct=_float("EXIT_COST_PCT", 1.5),
            ensemble_tactics=_list("ENSEMBLE_TACTICS", "est"),
            scalp_min_market_cap_usd=_float("SCALP_MIN_MARKET_CAP_USD", 0.0),
            scalp_max_market_cap_usd=_float("SCALP_MAX_MARKET_CAP_USD", 0.0),
            sol_price_usd=_float("SOL_PRICE_USD", 90.0),
            discovery_enabled=_bool("DISCOVERY_ENABLED", False),
            discovery_interval_sec=_float("DISCOVERY_INTERVAL_SEC", 60.0),
            discovery_min_mcap_usd=_float("DISCOVERY_MIN_MCAP_USD", 25_000.0),
            discovery_max_mcap_usd=_float("DISCOVERY_MAX_MCAP_USD", 10_000_000.0),
            discovery_min_h1_volume_usd=_float("DISCOVERY_MIN_H1_VOLUME_USD", 10_000.0),
            discovery_min_m5_txns=_int("DISCOVERY_MIN_M5_TXNS", 20),
            discovery_min_liquidity_usd=_float("DISCOVERY_MIN_LIQUIDITY_USD", 8_000.0),
            discovery_max_coins=_int("DISCOVERY_MAX_COINS", 60),
            require_holder_check=_bool("REQUIRE_HOLDER_CHECK", True),
            holder_check_ttl_sec=_float("HOLDER_CHECK_TTL_SEC", 300.0),
            established_idle_drop_sec=_float("ESTABLISHED_IDLE_DROP_SEC", 900.0),
            ensemble_top_k=_int("ENSEMBLE_TOP_K", 4),
            ensemble_window_min=_float("ENSEMBLE_WINDOW_MIN", 120.0),
            ensemble_explore_bonus=_float("ENSEMBLE_EXPLORE_BONUS", 10.0),
            ensemble_min_score=_float("ENSEMBLE_MIN_SCORE", 0.0),
            ensemble_max_open_positions=_int("ENSEMBLE_MAX_OPEN_POSITIONS", 6),
            ensemble_safety_max_hold_sec=_int("ENSEMBLE_SAFETY_MAX_HOLD_SEC", 900),
            ensemble_state_dir=_env("ENSEMBLE_STATE_DIR", "ensemble_state"),
            scalp_min_age_sec=_float("SCALP_MIN_AGE_SEC", d.scalp_min_age_sec),
            scalp_max_age_sec=_float("SCALP_MAX_AGE_SEC", d.scalp_max_age_sec),
            scalp_window_sec=_float("SCALP_WINDOW_SEC", d.scalp_window_sec),
            scalp_min_trades=_int("SCALP_MIN_TRADES", d.scalp_min_trades),
            scalp_min_unique_traders=_int("SCALP_MIN_UNIQUE_TRADERS", d.scalp_min_unique_traders),
            scalp_min_buy_ratio=_float("SCALP_MIN_BUY_RATIO", d.scalp_min_buy_ratio),
            scalp_min_net_inflow_sol=_float("SCALP_MIN_NET_INFLOW_SOL", d.scalp_min_net_inflow_sol),
            scalp_max_pump_pct=_float("SCALP_MAX_PUMP_PCT", d.scalp_max_pump_pct),
            scalp_min_score=_float("SCALP_MIN_SCORE", d.scalp_min_score),
            scalp_min_market_cap_sol=_float("SCALP_MIN_MARKET_CAP_SOL", d.scalp_min_market_cap_sol),
            scalp_max_market_cap_sol=_float("SCALP_MAX_MARKET_CAP_SOL", d.scalp_max_market_cap_sol),
            required_socials=_list("REQUIRED_SOCIALS", ",".join(d.required_socials)),
            socials_mode=_env("SOCIALS_MODE", d.socials_mode).lower(),
            ipfs_gateways=[g.strip() for g in _env("IPFS_GATEWAYS", ",".join(d.ipfs_gateways)).split(",") if g.strip()],
            rug_max_dev_pct=_float("RUG_MAX_DEV_PCT", d.rug_max_dev_pct),
            rug_max_top10_pct=_float("RUG_MAX_TOP10_PCT", d.rug_max_top10_pct),
            rug_max_single_holder_pct=_float("RUG_MAX_SINGLE_HOLDER_PCT", d.rug_max_single_holder_pct),
            rug_max_early_supply_pct=_float("RUG_MAX_EARLY_SUPPLY_PCT", d.rug_max_early_supply_pct),
            rug_min_trader_diversity=_float("RUG_MIN_TRADER_DIVERSITY", d.rug_min_trader_diversity),
            rug_max_creator_launches_per_hour=_int(
                "RUG_MAX_CREATOR_LAUNCHES_PER_HOUR", d.rug_max_creator_launches_per_hour
            ),
            rug_dump_exit_pct=_float("RUG_DUMP_EXIT_PCT", d.rug_dump_exit_pct),
            scalp_max_hold_sec=_int("SCALP_MAX_HOLD_SEC", d.scalp_max_hold_sec),
            scalp_fade_window_sec=_float("SCALP_FADE_WINDOW_SEC", d.scalp_fade_window_sec),
            scalp_fade_sell_ratio=_float("SCALP_FADE_SELL_RATIO", d.scalp_fade_sell_ratio),
            scalp_reentry_cooldown_sec=_int("SCALP_REENTRY_COOLDOWN_SEC", d.scalp_reentry_cooldown_sec),
            scalp_max_entries_per_token=_int("SCALP_MAX_ENTRIES_PER_TOKEN", d.scalp_max_entries_per_token),
            scalp_max_open_positions=_int("SCALP_MAX_OPEN_POSITIONS", d.scalp_max_open_positions),
            scalp_eval_interval_sec=_float("SCALP_EVAL_INTERVAL_SEC", d.scalp_eval_interval_sec),
            scalp_require_new_high=_bool("SCALP_REQUIRE_NEW_HIGH", False),
            scalp_dip_min_pct=_float("SCALP_DIP_MIN_PCT", 0.0),
            scalp_dip_max_pct=_float("SCALP_DIP_MAX_PCT", 0.0),
            scalp_dip_window_sec=_float("SCALP_DIP_WINDOW_SEC", 180.0),
            dry_run=_bool("DRY_RUN", True),
            paper_balance_sol=_float("PAPER_BALANCE_SOL", 5.0),
            ws_url=_env("WS_URL", cls.ws_url),
            trade_url=_env("TRADE_URL", cls.trade_url),
            rpc_url=_env("RPC_URL", cls.rpc_url),
            private_key=_env("PRIVATE_KEY", ""),
            api_key=_env("PUMPPORTAL_API_KEY", ""),
            top_n_traders=_int("TOP_N_TRADERS", 25),
            leaderboard_window_hours=_float("LEADERBOARD_WINDOW_HOURS", 24.0),
            min_round_trips=_int("MIN_ROUND_TRIPS", 3),
            min_median_hold_sec=_float("MIN_MEDIAN_HOLD_SEC", 120.0),
            leaderboard_refresh_sec=_int("LEADERBOARD_REFRESH_SEC", 300),
            max_tracked_tokens=_int("MAX_TRACKED_TOKENS", 400),
            seed_traders_file=_env("SEED_TRADERS_FILE", "traders.txt"),
            exclude_wallets=excl,
            take_profit_pct=_float("TAKE_PROFIT_PCT", 20.0),
            stop_loss_pct=_float("STOP_LOSS_PCT", 12.0),
            max_hold_sec=_int("MAX_HOLD_SEC", 1200),
            exit_on_leader_sell=_bool("EXIT_ON_LEADER_SELL", True),
            let_winners_run=_bool("LET_WINNERS_RUN", True),
            trailing_stop_pct=_float("TRAILING_STOP_PCT", 10.0),
            tp_floor_pct=_float("TP_FLOOR_PCT", 20.0),
            runner_max_hold_sec=_int("RUNNER_MAX_HOLD_SEC", 3600),
            min_win_prob=_float("MIN_WIN_PROB", 0.55),
            min_expected_value_pct=_float("MIN_EXPECTED_VALUE_PCT", 3.0),
            max_chase_pct=_float("MAX_CHASE_PCT", 8.0),
            confluence_window_sec=_int("CONFLUENCE_WINDOW_SEC", 300),
            confluence_bonus=_float("CONFLUENCE_BONUS", 0.15),
            min_leader_buy_sol=_float("MIN_LEADER_BUY_SOL", 0.3),
            min_market_cap_sol=_float("MIN_MARKET_CAP_SOL", 25.0),
            max_market_cap_sol=_float("MAX_MARKET_CAP_SOL", 400.0),
            outcome_horizon_sec=_int("OUTCOME_HORIZON_SEC", 1800),
            buy_amount_sol=_float("BUY_AMOUNT_SOL", 0.1),
            max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
            max_daily_loss_sol=_float("MAX_DAILY_LOSS_SOL", 0.5),
            mint_cooldown_sec=_int("MINT_COOLDOWN_SEC", 1800),
            slippage_pct=_float("SLIPPAGE_PCT", 5.0),
            priority_fee_sol=_float("PRIORITY_FEE_SOL", 0.0005),
            fee_pct=_float("FEE_PCT", 1.0),
            pool=_env("POOL", "auto"),
            confirm_timeout_sec=_float("CONFIRM_TIMEOUT_SEC", 45.0),
            paper_latency_sec=_float("PAPER_LATENCY_SEC", 1.5),
            paper_slippage_pct=_float("PAPER_SLIPPAGE_PCT", 0.5),
            state_file=_env("STATE_FILE", "state.json"),
            log_level=_env("LOG_LEVEL", "INFO"),
        )
