import asyncio

import pytest

from pumpbot.ensemble import EnsembleBot, tactic_score
from pumpbot.executor import PaperExecutor
from pumpbot.positions import ClosedTrade

from test_scalper import DEV, FakeFeed, FakeSocials, GOOD_META, cfg, launch, msg, organic_pump

TACTICS = {
    "eager": {},  # default scalp rules: buys the organic pump in these tests
    "picky": {"scalp_min_trades": 500},  # never buys
}


def make(tmp_path, tactics=TACTICS, **kw):
    c = cfg(tmp_path, ensemble_state_dir=str(tmp_path / "ens"), ensemble_tactics=["all"], **kw)
    now = [0.0]
    bot = EnsembleBot(c, PaperExecutor(c), feed=FakeFeed(), clock=lambda: now[0], socials=FakeSocials(GOOD_META),
                      tactics=tactics)
    return bot, now


async def drain_all(bot):
    await bot.drain_all()


def test_score_rewards_winners_and_explores_unknowns():
    assert tactic_score([], 10) == pytest.approx(10)
    winner = tactic_score([20, 15, 30], 10)
    loser = tactic_score([-15, -12, -18], 10)
    assert winner > tactic_score([], 10) > loser
    assert loser < 0  # a proven loser drops out


def test_ensemble_follows_active_tactic_buy_and_exit(tmp_path):
    bot, now = make(tmp_path)

    async def go():
        bot.rank()
        assert set(bot.active) == {"eager", "picky"}
        await launch(bot, now)
        await drain_all(bot)
        price = await organic_pump(bot, now)
        await drain_all(bot)
        assert "MINT" in bot.shadows["eager"].positions.open
        assert "MINT" in bot.positions.open and bot.owner["MINT"] == "eager"
        # the shadow's stop loss closes the real position too
        now[0] += 1
        await bot.handle(msg("x", "MINT", "sell", 0.1, 1_000_000, price * 0.7, "dump"))
        await drain_all(bot)
        assert "MINT" not in bot.shadows["eager"].positions.open
        assert "MINT" not in bot.positions.open
        assert bot.positions.closed[-1].reason.startswith("[eager]")

    asyncio.run(go())


def test_losing_tactic_is_dropped_and_not_followed(tmp_path):
    bot, now = make(tmp_path)
    shadow = bot.shadows["eager"]
    for i in range(6):  # a history of losses
        shadow.positions.closed.append(ClosedTrade(f"m{i}", 0.1, 0.08, 0, 0, "stop-loss"))

    async def go():
        bot.rank()
        assert bot.active == ["picky"]
        await launch(bot, now)
        await drain_all(bot)
        await organic_pump(bot, now)
        await drain_all(bot)
        assert "MINT" in shadow.positions.open  # the shadow still trades (and keeps learning)
        assert not bot.positions.open  # but the real wallet ignores it

    asyncio.run(go())


def test_top_k_limits_active_tactics(tmp_path):
    bot, _ = make(tmp_path, tactics={"a": {}, "b": {}, "c": {}}, ensemble_top_k=2)
    bot.shadows["c"].positions.closed.append(ClosedTrade("m", 0.1, 0.15, 0, 0, "runner"))
    bot.rank()
    assert bot.active[0] == "c" and len(bot.active) == 2


def test_orphan_position_is_closed_when_tactic_buy_failed(tmp_path):
    bot, now = make(tmp_path)

    async def go():
        bot.rank()
        await launch(bot, now)
        await drain_all(bot)
        await organic_pump(bot, now)
        await drain_all(bot)
        assert "MINT" in bot.positions.open
        bot.shadows["eager"].positions.open.pop("MINT")  # simulate the tactic's own fill failing
        await bot.tick()
        await drain_all(bot)
        assert "MINT" not in bot.positions.open
        assert "no longer holds" in bot.positions.closed[-1].reason

    asyncio.run(go())


def test_safety_timeout(tmp_path):
    bot, now = make(tmp_path, ensemble_safety_max_hold_sec=100)

    async def go():
        bot.rank()
        await launch(bot, now)
        await drain_all(bot)
        await organic_pump(bot, now)
        await drain_all(bot)
        assert "MINT" in bot.positions.open
        now[0] += 200
        await bot.tick()
        await drain_all(bot)
        assert "MINT" not in bot.positions.open

    asyncio.run(go())
