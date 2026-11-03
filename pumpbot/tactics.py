"""Named scalp tactics: each is a set of config overrides.

Used by the ensemble strategy (all tactics compete, the best ones trade),
by the experiment runner, and by TACTIC=<name> in the main bot.
"""

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
    # ---- round 3: buy accumulation BEFORE the spike (round 2 bought after +20-95% run-ups) ----
    "calm_accumulation": dict(
        scalp_window_sec=30, scalp_max_pump_pct=15, scalp_min_trades=10, scalp_min_unique_traders=6,
        scalp_min_buy_ratio=0.6, scalp_min_net_inflow_sol=0.5, scalp_min_score=30,
        scalp_min_market_cap_sol=15, scalp_max_market_cap_sol=800,
        take_profit_pct=12, tp_floor_pct=12, trailing_stop_pct=5, stop_loss_pct=10,
        scalp_max_hold_sec=180, scalp_fade_sell_ratio=0.85,
    ),
    "calm_runner": dict(
        scalp_window_sec=30, scalp_max_pump_pct=15, scalp_min_trades=10, scalp_min_unique_traders=6,
        scalp_min_buy_ratio=0.6, scalp_min_net_inflow_sol=0.5, scalp_min_score=30,
        scalp_min_market_cap_sol=15, scalp_max_market_cap_sol=800,
        take_profit_pct=20, tp_floor_pct=15, trailing_stop_pct=10, stop_loss_pct=12,
        scalp_max_hold_sec=300, scalp_fade_sell_ratio=1.01,
    ),
    "calm_strict": dict(
        scalp_window_sec=60, scalp_max_pump_pct=10, scalp_min_trades=20, scalp_min_unique_traders=12,
        scalp_min_buy_ratio=0.65, scalp_min_net_inflow_sol=1.0, scalp_min_score=40,
        scalp_min_market_cap_sol=20, scalp_max_market_cap_sol=600,
        take_profit_pct=15, tp_floor_pct=15, trailing_stop_pct=7, stop_loss_pct=10,
        scalp_max_hold_sec=240, scalp_fade_sell_ratio=0.85,
    ),
}

# ---------------------------------------------------------------------------
# Established coins: only trade coins already worth $25k+ (the late bonding
# curve and coins that graduated to PumpSwap). Coins come from two places:
# coins the bot watched from launch that grew past $25k, and coins found on
# DexScreener. Socials and rug checks still apply. Coins found mid-life need
# an RPC holder check (REQUIRE_HOLDER_CHECK).
_EST = dict(
    discovery_enabled=True, scalp_min_market_cap_usd=25_000, scalp_max_market_cap_usd=10_000_000,
    scalp_min_age_sec=60, scalp_max_age_sec=1e9, stop_loss_pct=8, scalp_max_hold_sec=300,
    scalp_max_pump_pct=60, scalp_reentry_cooldown_sec=120, scalp_max_entries_per_token=5,
    scalp_fade_sell_ratio=0.75,
)

TACTICS.update({
    # steady demand: lots of trades, mostly buying, money flowing in
    "est_momentum": dict(_EST, scalp_window_sec=60, scalp_min_trades=20, scalp_min_unique_traders=12,
                         scalp_min_buy_ratio=0.6, scalp_min_net_inflow_sol=1.0, scalp_min_score=50),
    # same entries, but no early sell when selling picks up; wider stop, longer hold
    "est_runner": dict(_EST, scalp_window_sec=60, scalp_min_trades=20, scalp_min_unique_traders=12,
                       scalp_min_buy_ratio=0.6, scalp_min_net_inflow_sol=1.0, scalp_min_score=50,
                       scalp_fade_sell_ratio=1.01, stop_loss_pct=10, scalp_max_hold_sec=600, trailing_stop_pct=12),
    # buy at a fresh high
    "est_breakout": dict(_EST, scalp_require_new_high=True, scalp_window_sec=60, scalp_min_trades=15,
                         scalp_min_unique_traders=10, scalp_min_buy_ratio=0.55, scalp_min_net_inflow_sol=0.5,
                         scalp_min_score=40),
    # buy an 8-25% pullback from the 5-minute high while trading stays busy
    "est_dip": dict(_EST, scalp_dip_min_pct=8, scalp_dip_max_pct=25, scalp_dip_window_sec=300,
                    scalp_min_trades=12, scalp_min_unique_traders=8, scalp_min_buy_ratio=0.5,
                    scalp_min_net_inflow_sol=-2.0, scalp_min_score=30, stop_loss_pct=10),
    # wherever trading is busiest
    "est_volume": dict(_EST, scalp_window_sec=30, scalp_min_trades=25, scalp_min_unique_traders=12,
                       scalp_min_buy_ratio=0.55, scalp_min_net_inflow_sol=0.0, scalp_min_score=0),
    # buying builds up but price hasn't moved yet (<8%)
    "est_calm": dict(_EST, scalp_window_sec=60, scalp_max_pump_pct=8, scalp_min_trades=15,
                     scalp_min_unique_traders=10, scalp_min_buy_ratio=0.6, scalp_min_net_inflow_sol=0.5,
                     scalp_min_score=30),
    # the run-up to graduation: $25k-$42k, the last stretch of the bonding curve
    "est_graduation": dict(_EST, scalp_max_market_cap_usd=42_000, scalp_window_sec=60, scalp_min_trades=15,
                           scalp_min_unique_traders=10, scalp_min_buy_ratio=0.6, scalp_min_net_inflow_sol=1.0,
                           scalp_min_score=40),
})
