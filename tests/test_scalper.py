import asyncio

import pytest

from pumpbot.config import Config
from pumpbot.executor import PaperExecutor
from pumpbot.potential import PotentialAnalyzer
from pumpbot.scalper import ScalperBot
from pumpbot.socials import SocialsChecker, check_socials, classify, extract_socials, gateway_urls
from pumpbot.tokens import TOTAL_SUPPLY, TokenState

V_TOK = 1_000_000_000.0
DEV = "DEVwallet"


def cfg(tmp_path=None, **kw):
    c = Config(
        state_file=str(tmp_path / "s.json") if tmp_path else "s.json",
        paper_latency_sec=0,
        paper_slippage_pct=0,
        scalp_min_age_sec=20,
        scalp_eval_interval_sec=0,
    )
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def msg(trader, mint, side, sol, tokens, price, sig, mcap=60.0, **extra):
    m = {
        "signature": sig, "mint": mint, "traderPublicKey": trader, "txType": side,
        "solAmount": sol, "tokenAmount": tokens, "vSolInBondingCurve": price * V_TOK,
        "vTokensInBondingCurve": V_TOK, "marketCapSol": mcap,
    }
    m.update(extra)
    return m


# ----------------------------------------------------------------- socials
def test_classify_links():
    assert classify("https://x.com/realproject") == ("twitter", "x.com/realproject")
    assert classify("https://twitter.com/RealProject") == ("twitter", "x.com/realproject")
    assert classify("https://x.com/") is None  # bare x.com proves nothing
    assert classify("https://x.com/search?q=coin") is None
    assert classify("https://x.com/elonmusk/status/123") is None  # someone's tweet, not the coin's X
    assert classify("https://x.com/i/communities/1789") == ("twitter", "x.com/i/communities/1789")
    assert classify("https://instagram.com/p/abc123") is None  # a post, not a profile
    assert classify("https://www.instagram.com/coolcoin") == ("instagram", "instagram.com/coolcoin")
    assert classify("https://coolcoin.xyz/about") == ("website", "coolcoin.xyz")
    assert classify("https://pump.fun/coin/abc") is None
    assert classify("https://dexscreener.com/solana/abc") is None


def test_extract_socials_from_fields_and_description():
    meta = {
        "twitter": "https://x.com/coolcoin",
        "website": "",
        "description": "the coolest coin. follow us https://instagram.com/coolcoin and www.coolcoin.io",
    }
    s = extract_socials(meta)
    assert s == {"twitter": "x.com/coolcoin", "instagram": "instagram.com/coolcoin", "website": "coolcoin.io"}


def test_check_socials_modes():
    req = ["website", "twitter", "instagram"]
    assert check_socials({"twitter": "x.com/a"}, req, "any")[0]
    assert not check_socials({"telegram": "t.me/a"}, req, "any")[0]
    assert not check_socials({"twitter": "x.com/a"}, req, "all")[0]
    assert not check_socials(None, req, "any")[0]


def test_gateway_fallbacks():
    gws = ["https://a/ipfs/{cid}", "https://b/ipfs/{cid}"]
    assert gateway_urls("https://ipfs.io/ipfs/QmX", gws) == ["https://a/ipfs/QmX", "https://b/ipfs/QmX"]
    assert gateway_urls("https://x.mypinata.cloud/ipfs/QmX", gws)[0] == "https://x.mypinata.cloud/ipfs/QmX"
    assert gateway_urls("https://meta.example/abc.json", gws) == ["https://meta.example/abc.json"]


def test_reused_links_are_detected():
    sc = SocialsChecker([])
    assert sc.reused_links("mintA", {"twitter": "x.com/real"}) == []
    assert sc.reused_links("mintA", {"twitter": "x.com/real"}) == []  # same coin is fine
    assert sc.reused_links("mintB", {"twitter": "x.com/real"}) == ["twitter x.com/real"]


# ------------------------------------------------------------- token state
def _token(dev_tokens=20_000_000.0):
    t = TokenState("MINT", DEV, created_at=0.0)
    from pumpbot.models import TradeEvent

    t.on_trade(TradeEvent.from_pumpportal(msg(DEV, "MINT", "create", 0.5, dev_tokens, 3e-8, "c"), now=0.0))
    return t


def test_holder_and_dev_tracking():
    t = _token(dev_tokens=50_000_000)  # 5%
    assert t.dev_pct == pytest.approx(5.0)
    assert t.early_supply_pct == pytest.approx(5.0)
    assert t.top_holders_pct(10) == pytest.approx(5.0)


# ------------------------------------------------------------- end-to-end
class FakeFeed:
    def __init__(self):
        self.tokens, self.accounts = set(), set()

    async def subscribe_tokens(self, m):
        self.tokens |= set(m)

    async def unsubscribe_tokens(self, m):
        self.tokens -= set(m)

    async def set_accounts(self, w):
        self.accounts = set(w)


class FakeSocials(SocialsChecker):
    def __init__(self, meta):
        super().__init__([])
        self.meta = meta

    async def fetch_metadata(self, uri):
        return self.meta


GOOD_META = {"twitter": "https://x.com/goodcoin", "website": "https://goodcoin.xyz"}


def make_bot(tmp_path, meta=GOOD_META, **kw):
    c = cfg(tmp_path, **kw)
    now = [0.0]
    bot = ScalperBot(c, PaperExecutor(c), feed=FakeFeed(), clock=lambda: now[0], socials=FakeSocials(meta))
    return bot, now


async def launch(bot, now, mint="MINT", dev_tokens=20_000_000.0, early_whales=0):
    now[0] = 0.0
    await bot.handle(msg(DEV, mint, "create", 0.5, dev_tokens, 3e-8, f"{mint}-c", name="Good", symbol="GOOD", uri="u"))
    for i in range(early_whales):
        await bot.handle(msg(f"bundle{i}", mint, "buy", 3.0, 100_000_000, 3.2e-8, f"{mint}-b{i}"))
    await bot.drain()  # socials fetched


async def organic_pump(bot, now, mint="MINT", start=25.0, n=40, price0=3e-8, step=1.005, tag="t"):
    """n trades from many different wallets, mostly buys, rising price."""
    price = price0
    for i in range(n):
        now[0] = start + i * 0.8
        price *= step
        side = "sell" if i % 6 == 5 else "buy"
        await bot.handle(msg(f"w{i % 25}", mint, side, 0.4, 1_000_000, price, f"{mint}-{tag}{i}"))
    return price


def test_scalper_buys_strong_coin_with_socials_and_takes_profit(tmp_path):
    bot, now = make_bot(tmp_path)

    async def go():
        await launch(bot, now)
        price = await organic_pump(bot, now)
        await bot.drain()
        assert "MINT" in bot.positions.open, bot.skip_reasons
        entry = bot.positions.open["MINT"].entry_price
        # pump past +20% -> runner, then a pullback triggers the trailing stop
        now[0] += 1
        await bot.handle(msg("buyer", "MINT", "buy", 2.0, 1_000_000, entry * 1.4, "pump"))
        assert bot.positions.open["MINT"].runner
        now[0] += 1
        await bot.handle(msg("seller", "MINT", "sell", 0.5, 1_000_000, entry * 1.24, "pb"))
        await bot.drain()
        assert "MINT" not in bot.positions.open
        trade = bot.positions.closed[-1]
        assert trade.pnl_sol > 0 and "runner" in trade.reason

    asyncio.run(go())


def test_no_socials_no_buy(tmp_path):
    bot, now = make_bot(tmp_path, meta={"description": "just a coin", "telegram": "https://t.me/x"})

    async def go():
        await launch(bot, now)
        await organic_pump(bot, now)
        await bot.drain()
        assert not bot.positions.open
        assert any("no website/twitter/instagram link" in k for k in bot.skip_reasons)

    asyncio.run(go())


def test_metadata_unavailable_no_buy(tmp_path):
    bot, now = make_bot(tmp_path, meta=None)

    async def go():
        await launch(bot, now)
        await organic_pump(bot, now)
        await bot.drain()
        assert not bot.positions.open

    asyncio.run(go())


def test_bundled_launch_is_flagged(tmp_path):
    bot, now = make_bot(tmp_path)

    async def go():
        await launch(bot, now, early_whales=3)  # 30% bought in the first 3s
        await organic_pump(bot, now)
        await bot.drain()
        assert not bot.positions.open
        assert "bundled launch" in bot.tokens["MINT"].flag

    asyncio.run(go())


def test_dev_heavy_bag_blocks_buy(tmp_path):
    bot, now = make_bot(tmp_path)

    async def go():
        await launch(bot, now, dev_tokens=150_000_000)  # dev holds 15%
        await organic_pump(bot, now)
        await bot.drain()
        assert not bot.positions.open
        assert any("dev holds" in k for k in bot.skip_reasons)

    asyncio.run(go())


def test_dev_sell_flags_coin_blacklists_creator_and_exits(tmp_path):
    bot, now = make_bot(tmp_path)

    async def go():
        await launch(bot, now)
        await organic_pump(bot, now)
        await bot.drain()
        assert "MINT" in bot.positions.open
        await bot.handle(msg(DEV, "MINT", "sell", 0.5, 5_000_000, 3.1e-8, "devsell"))
        await bot.drain()
        assert "MINT" not in bot.positions.open
        assert bot.positions.closed[-1].reason.startswith("dev is selling")
        assert DEV in bot.bad_creators
        # the same creator's next coin is never bought
        await launch(bot, now, mint="MINT2")
        await organic_pump(bot, now, mint="MINT2")
        await bot.drain()
        assert "MINT2" not in bot.positions.open
        assert "creator blacklisted" in bot.tokens["MINT2"].flag

    asyncio.run(go())


def test_wash_trading_is_rejected(tmp_path):
    bot, now = make_bot(tmp_path, scalp_min_unique_traders=2)

    async def go():
        await launch(bot, now)
        price = 3e-8
        for i in range(40):  # 3 wallets making 40 trades
            now[0] = 25 + i * 0.8
            price *= 1.005
            await bot.handle(msg(f"bot{i % 3}", "MINT", "buy", 0.4, 1_000_000, price, f"wt{i}"))
        await bot.drain()
        assert not bot.positions.open
        assert any("wash trading" in k for k in bot.skip_reasons)

    asyncio.run(go())


def test_momentum_fade_exit(tmp_path):
    bot, now = make_bot(tmp_path)

    async def go():
        await launch(bot, now)
        price = await organic_pump(bot, now)
        await bot.drain()
        assert "MINT" in bot.positions.open
        for i in range(8):  # heavy selling, price barely moves
            now[0] += 0.5
            await bot.handle(msg(f"s{i}", "MINT", "sell", 1.5, 1_000_000, price * 0.99, f"fade{i}"))
        await bot.drain()
        assert "MINT" not in bot.positions.open
        assert bot.positions.closed[-1].reason.startswith("momentum faded")

    asyncio.run(go())


def test_scalper_can_reenter_after_cooldown_up_to_limit(tmp_path):
    bot, now = make_bot(tmp_path, scalp_reentry_cooldown_sec=10, scalp_max_entries_per_token=2)

    async def go():
        await launch(bot, now)
        price = await organic_pump(bot, now)
        await bot.drain()
        assert bot.tokens["MINT"].entries == 1
        now[0] += 1
        await bot.handle(msg("x", "MINT", "sell", 0.1, 1_000_000, price * 0.8, "sl"))  # stop loss
        await bot.drain()
        assert "MINT" not in bot.positions.open
        await organic_pump(bot, now, start=now[0] + 15, price0=price * 0.8, tag="second")
        await bot.drain()
        assert bot.tokens["MINT"].entries == 2

    asyncio.run(go())


def test_low_activity_not_bought(tmp_path):
    bot, now = make_bot(tmp_path)

    async def go():
        await launch(bot, now)
        await organic_pump(bot, now, n=10)
        await bot.drain()
        assert not bot.positions.open

    asyncio.run(go())


def test_supply_constant():
    assert TOTAL_SUPPLY == 1e9
    assert PotentialAnalyzer(cfg())  # constructs


# ------------------------------------------------------------ entry styles
def test_breakout_requires_new_high(tmp_path):
    from pumpbot.models import TradeEvent

    c = cfg(tmp_path, scalp_require_new_high=True)
    an = PotentialAnalyzer(c)
    t = _token()
    t.socials = {"twitter": "x.com/good"}
    price = 3e-8
    for i in range(40):
        price *= 1.005
        side = "sell" if i % 6 == 5 else "buy"
        t.on_trade(TradeEvent.from_pumpportal(msg(f"w{i % 25}", "MINT", side, 0.4, 1_000_000, price, f"x{i}"), now=25 + i * 0.8))
    assert an.assess(t, 57.0).approved  # rising: at the high -> breakout entry ok
    t.on_trade(TradeEvent.from_pumpportal(msg("w99", "MINT", "buy", 0.4, 1_000_000, price * 0.9, "lo"), now=57.5))
    a = an.assess(t, 57.5)
    assert not a.approved and "not at a new high" in a.reasons


def test_dip_buyer_waits_for_pullback(tmp_path):
    bot, now = make_bot(tmp_path, scalp_dip_min_pct=15, scalp_dip_max_pct=40)

    async def go():
        await launch(bot, now)
        price = await organic_pump(bot, now)
        await bot.drain()
        assert not bot.positions.open  # at the high: no dip yet
        now[0] += 0.5
        await bot.handle(msg("w98", "MINT", "buy", 0.4, 1_000_000, price * 0.75, "dip"))  # -25%
        await bot.drain()
        assert "MINT" in bot.positions.open

    asyncio.run(go())


def test_experiment_tactics_are_valid_config():
    from dataclasses import replace

    from pumpbot.experiment import TACTICS, table

    for name, overrides in TACTICS.items():
        replace(Config(), **overrides)  # raises on unknown fields
    assert "tactic" in table([])
