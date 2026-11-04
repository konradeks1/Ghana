import asyncio

import pytest

from pumpbot.analyzer import EntryAnalyzer
from pumpbot.bot import CopyTradeBot
from pumpbot.config import Config
from pumpbot.executor import PaperExecutor
from pumpbot.leaderboard import Leaderboard
from pumpbot.models import TradeEvent
from pumpbot.outcomes import EntryOutcomeTracker
from pumpbot.positions import Position, PositionManager

V_TOK = 1_000_000_000.0


def msg(trader, mint, side, sol, tokens, price, sig, mcap=50.0):
    return {
        "signature": sig,
        "mint": mint,
        "traderPublicKey": trader,
        "txType": side,
        "solAmount": sol,
        "tokenAmount": tokens,
        "vSolInBondingCurve": price * V_TOK,
        "vTokensInBondingCurve": V_TOK,
        "marketCapSol": mcap,
    }


def evt(trader, mint, side, sol, tokens, price, ts, mcap=50.0):
    return TradeEvent.from_pumpportal(msg(trader, mint, side, sol, tokens, price, f"{trader}{mint}{ts}", mcap), now=ts)


class FakeFeed:
    def __init__(self):
        self.tokens, self.accounts = set(), set()

    async def subscribe_tokens(self, m):
        self.tokens |= set(m)

    async def unsubscribe_tokens(self, m):
        self.tokens -= set(m)

    async def set_accounts(self, w):
        self.accounts = set(w)


def cfg(tmp_path=None, **kw):
    c = Config(
        seed_traders_file="",
        state_file=str(tmp_path / "s.json") if tmp_path else "s.json",
        paper_latency_sec=0,
        paper_slippage_pct=0,
        min_median_hold_sec=0,
        any_profit_mode=False,  # these tests cover the classic +TP / floor rules
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# --------------------------------------------------------------- parsing
def test_parse_price_from_curve():
    e = evt("w", "m", "buy", 1.0, 1000, 3e-8, 0)
    assert e.price == pytest.approx(3e-8)
    assert TradeEvent.from_pumpportal({"txType": "buy"}) is None


# ----------------------------------------------------------- leaderboard
def test_leaderboard_realized_pnl_and_ranking():
    lb = Leaderboard(window_hours=24, min_round_trips=2)
    for i, (w, out) in enumerate([("good", 1.5), ("good", 1.3), ("bad", 0.5), ("bad", 0.7)]):
        m = f"mint{i}"
        lb.on_trade(evt(w, m, "buy", 1.0, 1000, 1e-3, 100 + i))
        lb.on_trade(evt(w, m, "sell", out, 1000, 1e-3, 200 + i))
    good = lb.get("good")
    assert good.pnl() == pytest.approx(0.8)
    assert len(good.round_trips) == 2
    assert good.tp_rate(20)[0] == 2
    top = lb.top(25, now=300)
    assert [s.wallet for s in top] == ["good"]  # losers never rank


def test_partial_sells_close_round_trip_once():
    lb = Leaderboard(min_round_trips=1)
    lb.on_trade(evt("w", "m", "buy", 1.0, 1000, 1e-3, 1))
    lb.on_trade(evt("w", "m", "sell", 0.6, 500, 1e-3, 2))
    assert lb.get("w").round_trips == type(lb.get("w").round_trips)()
    lb.on_trade(evt("w", "m", "sell", 0.6, 500, 1e-3, 3))
    (_, ret, hold), = lb.get("w").round_trips
    assert ret == pytest.approx(20.0) and hold == 2


def test_sell_without_known_buy_is_ignored():
    lb = Leaderboard()
    lb.on_trade(evt("w", "m", "sell", 5.0, 1000, 1e-3, 1))
    assert lb.get("w").pnl() == 0


# --------------------------------------------------------------- outcomes
def test_outcome_tracker_hits_and_misses():
    t = EntryOutcomeTracker(tp_pct=20, sl_pct=10, horizon_sec=100)
    t.add("a", "m", 1.0, 0)
    t.add("b", "m2", 1.0, 0)
    t.add("c", "m3", 1.0, 0)
    assert t.on_price("m", 1.1, 1) == []
    assert t.on_price("m", 1.21, 2) == [("a", True)]
    assert t.on_price("m2", 0.89, 2) == [("b", False)]
    assert t.expire(500) == [("c", False)]
    assert len(t) == 0


# --------------------------------------------------------------- analyzer
def _proven_trader(lb, wallet, hits=8, misses=1):
    s = lb.get(wallet)
    s.copy_hits, s.copy_misses = hits, misses
    s.buy_sizes.extend([1.0] * 5)
    return s


def test_analyzer_approves_proven_trader():
    c = cfg()
    lb = Leaderboard()
    _proven_trader(lb, "pro")
    a = EntryAnalyzer(c, lb)
    sig = a.evaluate(evt("pro", "m", "buy", 1.0, 1000, 1e-6, 10), 1e-6, {"pro"})
    assert sig.approved, sig.summary()
    assert sig.expected_value_pct > 0


def test_analyzer_rejects_unknown_trader_and_chasing():
    c = cfg()
    lb = Leaderboard()
    _proven_trader(lb, "pro")
    a = EntryAnalyzer(c, lb)
    assert not a.evaluate(evt("newbie", "m", "buy", 1.0, 1000, 1e-6, 10), 1e-6, {"newbie"}).approved
    chased = a.evaluate(evt("pro", "m", "buy", 1.0, 1000, 1e-6, 10), 1.2e-6, {"pro"})
    assert not chased.approved and "past leader entry" in chased.reasons[0]
    assert not a.evaluate(evt("pro", "m", "buy", 1.0, 1000, 1e-6, 10, mcap=5000), 1e-6, {"pro"}).approved


def test_confluence_raises_probability():
    c = cfg()
    lb = Leaderboard()
    for w in ("a", "b", "c"):
        _proven_trader(lb, w, hits=4, misses=3)
    an = EntryAnalyzer(c, lb)
    solo = an.evaluate(evt("a", "m", "buy", 1.0, 1000, 1e-6, 10), 1e-6, {"a", "b", "c"})
    an.record_buy(evt("b", "m", "buy", 1.0, 1000, 1e-6, 5))
    an.record_buy(evt("c", "m", "buy", 1.0, 1000, 1e-6, 6))
    group = an.evaluate(evt("a", "m", "buy", 1.0, 1000, 1e-6, 10), 1e-6, {"a", "b", "c"})
    assert group.probability > solo.probability
    assert len(group.leaders) == 3


# -------------------------------------------------------------- positions
def test_position_exits_fixed_tp():
    c = cfg(take_profit_pct=20, stop_loss_pct=10, max_hold_sec=60, let_winners_run=False)
    pm = PositionManager(c)
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    assert pm.exit_reason("m", 1.1, 1) is None
    assert pm.exit_reason("m", 1.2, 2).startswith("take-profit")
    assert pm.exit_reason("m", 0.9, 3).startswith("stop-loss")
    assert pm.exit_reason("m", 1.0, 61).startswith("timeout")


def test_winner_runs_past_tp_with_trailing_stop():
    c = cfg(take_profit_pct=20, stop_loss_pct=10, trailing_stop_pct=10, tp_floor_pct=20, max_hold_sec=60)
    pm = PositionManager(c)
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    assert pm.exit_reason("m", 1.25, 1) is None  # hits +TP -> becomes a runner
    assert pm.open["m"].runner
    assert pm.exit_reason("m", 2.0, 100) is None  # no cap, no normal timeout
    assert pm.exit_reason("m", 1.85, 101) is None  # -7.5% from peak: hold
    r = pm.exit_reason("m", 1.79, 102)  # -10.5% from peak: trailing stop
    assert r.startswith("runner trailing-stop") and "peak +100.0%" in r


def test_runner_never_exits_below_floor():
    c = cfg(take_profit_pct=20, trailing_stop_pct=10, tp_floor_pct=20)
    pm = PositionManager(c)
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    pm.exit_reason("m", 1.22, 1)  # runner armed
    assert pm.exit_reason("m", 1.21, 2) is None  # trail would be 1.098, floor holds 1.20
    assert pm.exit_reason("m", 1.19, 3).startswith("runner floor")


def test_runner_timeout():
    c = cfg(runner_max_hold_sec=100)
    pm = PositionManager(c)
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    pm.exit_reason("m", 1.3, 10)
    assert pm.exit_reason("m", 1.3, 111).startswith("runner timeout")


def test_risk_limits():
    c = cfg(max_open_positions=1, max_daily_loss_sol=0.05)
    pm = PositionManager(c)
    assert pm.can_open("m", 0, 1.0)[0]
    assert not pm.can_open("m", 0, 0.01)[0]
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    assert pm.can_open("x", 0, 1.0) == (False, "max open positions")
    pm.close("m", 0.0, 10, "sl")
    assert pm.can_open("x", 20, 1.0) == (False, "daily loss limit hit")


# ------------------------------------------------------- end-to-end (paper)
def test_end_to_end_copy_trade_takes_profit(tmp_path):
    c = cfg(tmp_path, top_n_traders=25, min_round_trips=2)
    now = [1000.0]
    feed = FakeFeed()
    bot = CopyTradeBot(c, PaperExecutor(c), feed=feed, clock=lambda: now[0])

    async def scenario():
        # 1) the "pro" wallet makes profitable round trips -> ranks on the board
        for i in range(3):
            m = f"old{i}"
            await bot.handle(msg("pro", m, "create" if i == 0 else "buy", 1.0, 1000, 1e-6, f"b{i}"))
            await bot.handle(msg("pro", m, "sell", 1.4, 1000, 1.4e-6, f"s{i}"))
        bot.leaderboard.get("pro").copy_hits = 6
        await bot.refresh_followed()
        assert bot.followed == {"pro"} and feed.accounts == {"pro"}

        # 2) pro buys a fresh token -> bot analyses and copies
        await bot.handle(msg("pro", "MOON", "buy", 1.0, 1000, 1e-6, "entry"))
        await bot.drain()
        assert "MOON" in bot.positions.open
        entry = bot.positions.open["MOON"].entry_price

        # 3) price rises past +20% -> runner, keeps riding
        now[0] += 30
        await bot.handle(msg("rando", "MOON", "buy", 2.0, 1000, entry * 1.25, "pump"))
        assert bot.positions.open["MOON"].runner
        await bot.handle(msg("rando", "MOON", "buy", 2.0, 1000, entry * 1.8, "pump2"))
        # leader taking profit doesn't cut a runner short
        await bot.handle(msg("pro", "MOON", "sell", 1.0, 1000, entry * 1.75, "lsell"))
        assert "MOON" in bot.positions.open

        # 4) price falls >10% from the +80% peak -> trailing stop sells
        await bot.handle(msg("rando", "MOON", "sell", 2.0, 1000, entry * 1.6, "dump"))
        await bot.drain()
        assert "MOON" not in bot.positions.open
        trade = bot.positions.closed[-1]
        assert trade.reason.startswith("runner trailing-stop")
        assert trade.pnl_sol / trade.sol_in > 0.4

        await bot.maintenance()

    asyncio.run(scenario())
    assert (tmp_path / "s.json").exists()


def test_leader_sell_triggers_exit(tmp_path):
    c = cfg(tmp_path)
    feed = FakeFeed()
    bot = CopyTradeBot(c, PaperExecutor(c), feed=feed, clock=lambda: 50.0)
    _proven_trader(bot.leaderboard, "pro")
    bot.followed = {"pro"}

    async def scenario():
        await bot.handle(msg("pro", "M", "buy", 1.0, 1000, 1e-6, "a"))
        await bot.drain()
        assert "M" in bot.positions.open
        await bot.handle(msg("pro", "M", "sell", 1.0, 1000, 1.05e-6, "b"))
        await bot.drain()
        assert "M" not in bot.positions.open
        assert "leader" in bot.positions.closed[-1].reason

    asyncio.run(scenario())


# ------------------------------------------------------- scalper filter
def test_scalpers_are_not_followed():
    lb = Leaderboard(min_round_trips=2, min_median_hold_sec=120)
    for i in range(3):
        # scalper: flips in 5s; swing: holds 10 min. Both profitable.
        lb.on_trade(evt("scalper", f"a{i}", "buy", 1.0, 1000, 1e-3, 100 * i))
        lb.on_trade(evt("scalper", f"a{i}", "sell", 1.5, 1000, 1e-3, 100 * i + 5))
        lb.on_trade(evt("swing", f"b{i}", "buy", 1.0, 1000, 1e-3, 100 * i))
        lb.on_trade(evt("swing", f"b{i}", "sell", 1.3, 1000, 1e-3, 100 * i + 600))
    assert [s.wallet for s in lb.top(25, now=2000)] == ["swing"]


# --------------------------------------------------------- paper realism
def test_paper_fills_at_price_after_latency():
    c = cfg(paper_latency_sec=1.5, paper_slippage_pct=3, slippage_pct=10)
    market = {"m": 1.05}
    slept = []

    async def fake_sleep(t):
        slept.append(t)

    ex = PaperExecutor(c, price_fn=market.get, sleep=fake_sleep)
    fill = asyncio.run(ex.buy("m", 0.1, 1.0))
    assert slept == [1.5]
    assert fill.ok and fill.price > 1.05 * 1.03  # market moved + impact + fees

    market["m"] = 1.2  # +20% during latency > 10% slippage limit
    assert not asyncio.run(ex.buy("m", 0.1, 1.0)).ok

    market["m"] = 0.6  # -40% crash during latency: fails at the default 30% sell slippage...
    bad = asyncio.run(ex.sell("m", 100, 1.0))
    assert not bad.ok and "slippage" in bad.error
    assert asyncio.run(ex.sell("m", 100, 1.0, slippage_pct=50)).ok  # ...but a wider retry gets out


def test_failed_sell_is_retried(tmp_path):
    c = cfg(tmp_path)

    class FlakyPaper(PaperExecutor):
        fails = 1

        async def sell(self, mint, tokens, price, slippage_pct=None):
            if self.fails:
                self.fails -= 1
                from pumpbot.executor import Fill
                return Fill(False, 0, tokens, price, error="boom")
            return await super().sell(mint, tokens, price, slippage_pct)

    bot = CopyTradeBot(c, FlakyPaper(c), feed=FakeFeed(), clock=lambda: 50.0)
    _proven_trader(bot.leaderboard, "pro")
    bot.followed = {"pro"}

    async def scenario():
        await bot.handle(msg("pro", "M", "buy", 1.0, 1000, 1e-6, "a"))
        await bot.drain()
        await bot.handle(msg("x", "M", "sell", 1.0, 1000, 0.5e-6, "crash"))
        await bot.drain()
        assert "M" in bot.positions.open and "M" in bot._exit_retry
        await bot.tick()
        await bot.drain()
        assert "M" not in bot.positions.open and not bot._exit_retry

    asyncio.run(scenario())


def test_max_positions_counts_in_flight_buys(tmp_path):
    c = cfg(tmp_path, max_open_positions=1, paper_latency_sec=0.05)
    bot = CopyTradeBot(c, PaperExecutor(c), feed=FakeFeed(), clock=lambda: 50.0)
    _proven_trader(bot.leaderboard, "pro")
    bot.followed = {"pro"}

    async def scenario():
        await bot.handle(msg("pro", "A", "buy", 1.0, 1000, 1e-6, "a"))
        await bot.handle(msg("pro", "B", "buy", 1.0, 1000, 1e-6, "b"))  # A still in flight
        await bot.drain()
        assert set(bot.positions.open) == {"A"}

    asyncio.run(scenario())


# ---------------------------------------------------- live confirmation
def _live_executor(rpc_handler, send_result=("SIG", "")):
    from solders.keypair import Keypair
    from pumpbot.executor import PumpPortalExecutor

    ex = PumpPortalExecutor.__new__(PumpPortalExecutor)
    ex.cfg = cfg(confirm_timeout_sec=3)
    ex.keypair = Keypair()
    ex.pubkey = str(ex.keypair.pubkey())

    async def no_sleep(_):
        pass

    async def fake_send(*a, **k):
        return send_result

    async def fake_rpc(method, params):
        return rpc_handler(method, params)

    ex._sleep, ex._send, ex._rpc = no_sleep, fake_send, fake_rpc
    return ex


def _token_accounts(amount):
    info = {"tokenAmount": {"uiAmountString": str(amount)}}
    return {"value": [{"account": {"data": {"parsed": {"info": info}}}}]}


def test_live_buy_uses_confirmed_onchain_amounts():
    balances = iter([0, 123456.0])

    def rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return _token_accounts(next(balances))
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        if method == "getTransaction":
            return {"meta": {"preBalances": [1_000_000_000], "postBalances": [895_000_000]}}

    fill = asyncio.run(_live_executor(rpc).buy("m", 0.1, 1e-6))
    assert fill.ok and fill.tokens == 123456.0
    assert fill.sol == pytest.approx(0.105)  # real cost incl. fees
    assert fill.price == pytest.approx(0.105 / 123456.0)


def test_live_buy_failed_onchain_is_not_a_position():
    def rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return _token_accounts(0)
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "processed", "err": {"InstructionError": [3, {"Custom": 6003}]}}]}

    fill = asyncio.run(_live_executor(rpc).buy("m", 0.1, 1e-6))
    assert not fill.ok and "failed on-chain" in fill.error


def test_live_buy_timeout_without_tokens_fails():
    def rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return _token_accounts(0)
        if method == "getSignatureStatuses":
            return {"value": [None]}

    fill = asyncio.run(_live_executor(rpc).buy("m", 0.1, 1e-6))
    assert not fill.ok and fill.error == "no tokens received"


def test_live_sell_reports_real_proceeds():
    balances = iter([5000.0, 0.0])

    def rpc(method, params):
        if method == "getTokenAccountsByOwner":
            return _token_accounts(next(balances))
        if method == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "finalized", "err": None}]}
        if method == "getTransaction":
            return {"meta": {"preBalances": [1_000_000_000], "postBalances": [1_118_000_000]}}

    fill = asyncio.run(_live_executor(rpc).sell("m", 5000.0, 2e-5))
    assert fill.ok and fill.sol == pytest.approx(0.118)


# ------------------------------------------------------------ any-profit rule
def test_any_profit_locks_small_net_gain_and_lets_it_run():
    c = cfg(any_profit_mode=True, any_profit_arm_pct=3, any_profit_floor_pct=1, exit_cost_pct=1.5,
            trailing_stop_pct=10, stop_loss_pct=10)
    pm = PositionManager(c)
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    assert pm.exit_reason("m", 1.04, 1) is None and not pm.open["m"].runner  # +4% gross = +2.5% net
    assert pm.exit_reason("m", 1.05, 2) is None and pm.open["m"].runner  # +3.5% net: locked
    assert pm.exit_reason("m", 1.30, 3) is None  # still running
    r = pm.exit_reason("m", 1.16, 4)  # >10% below the 1.30 peak
    assert r.startswith("runner trailing-stop")


def test_any_profit_floor_keeps_trade_net_positive():
    c = cfg(any_profit_mode=True, any_profit_arm_pct=3, any_profit_floor_pct=1, exit_cost_pct=1.5)
    pm = PositionManager(c)
    pm.add(Position("m", 1.0, 0.1, 100, opened_at=0))
    pm.exit_reason("m", 1.05, 1)
    assert pm.exit_reason("m", 1.03, 2) is None  # +1.5% net, above the +1% floor
    assert pm.exit_reason("m", 1.024, 3).startswith("runner floor")  # +0.9% net


def test_sell_slippage_widens_until_it_gets_out_of_a_crash(tmp_path):
    """Regression: a 5% limit on sells left positions stuck while a coin dumped -99%."""
    c = cfg(tmp_path, paper_latency_sec=1.5, sell_slippage_pct=30, sell_slippage_step_pct=20,
            sell_slippage_max_pct=95)
    clock = [50.0]
    prices = {}

    async def sleep(dt):  # the price keeps collapsing during every fill delay
        prices["M"] = prices["M"] * 0.4

    bot = CopyTradeBot(c, PaperExecutor(c, sleep=sleep), feed=FakeFeed(), clock=lambda: clock[0])
    bot.executor.price_fn = prices.get
    from pumpbot.positions import Position

    bot.positions.add(Position("M", 1e-6, 0.1, 100_000, opened_at=0))
    prices["M"] = bot.prices["M"] = 1e-6

    async def go():
        await bot._exit("M", "stop-loss")
        await bot.drain()
        attempts = 1
        while "M" in bot.positions.open and attempts < 10:
            bot.prices["M"] = prices["M"]
            await bot.tick()
            await bot.drain()
            attempts += 1
        assert "M" not in bot.positions.open
        assert attempts <= 3  # 30% -> 50% -> 70%: out on the third try (-60% per delay)

    asyncio.run(go())
