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


def _bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # --- mode -------------------------------------------------------------
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
    slippage_pct: float = 10.0
    priority_fee_sol: float = 0.0005
    fee_pct: float = 1.0  # pump.fun fee per side
    pool: str = "auto"
    confirm_timeout_sec: float = 45.0  # live: wait this long for on-chain confirmation

    # --- paper realism ------------------------------------------------------
    paper_latency_sec: float = 1.5  # delay between decision and simulated fill
    paper_slippage_pct: float = 3.0  # extra price impact on every simulated fill

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
        return cls(
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
            slippage_pct=_float("SLIPPAGE_PCT", 10.0),
            priority_fee_sol=_float("PRIORITY_FEE_SOL", 0.0005),
            fee_pct=_float("FEE_PCT", 1.0),
            pool=_env("POOL", "auto"),
            confirm_timeout_sec=_float("CONFIRM_TIMEOUT_SEC", 45.0),
            paper_latency_sec=_float("PAPER_LATENCY_SEC", 1.5),
            paper_slippage_pct=_float("PAPER_SLIPPAGE_PCT", 3.0),
            state_file=_env("STATE_FILE", "state.json"),
            log_level=_env("LOG_LEVEL", "INFO"),
        )
