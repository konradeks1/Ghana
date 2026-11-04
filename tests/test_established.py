import asyncio

from pumpbot.market import DexDiscovery, MarketData, parse_pair
from pumpbot.models import TradeEvent
from pumpbot.potential import PotentialAnalyzer
from pumpbot.scalper import ScalperBot
from pumpbot.executor import PaperExecutor
from pumpbot.tactics import TACTICS

from test_scalper import FakeFeed, FakeSocials, GOOD_META, cfg, msg

MINT = "EstCoin1111111111111111111111111111111pump"


def amm_msg(trader, side, sol, tokens, sol_pool, tok_pool, sig, mcap_sol=400.0):
    return {"signature": sig, "mint": MINT, "traderPublicKey": trader, "txType": side, "solAmount": sol,
            "tokenAmount": tokens, "solInPool": sol_pool, "tokensInPool": tok_pool, "marketCapSol": mcap_sol,
            "pool": "pump-amm"}


PAIR = {
    "chainId": "solana", "dexId": "pumpswap", "pairAddress": "POOL1",
    "baseToken": {"address": MINT, "symbol": "EST", "name": "Established"},
    "marketCap": 120_000, "liquidity": {"usd": 30_000}, "volume": {"h1": 80_000},
    "txns": {"m5": {"buys": 40, "sells": 25}}, "pairCreatedAt": 1_000_000,
    "info": {"socials": [{"type": "twitter", "url": "https://x.com/estcoin"}],
             "websites": [{"url": "https://estcoin.io", "label": "Website"}]},
}


def test_pumpswap_trades_are_parsed():
    e = TradeEvent.from_pumpportal(amm_msg("w", "buy", 1.0, 1000, 135.0, 118_000_000, "s"), now=1)
    assert e.price == 135.0 / 118_000_000 and e.market_cap_sol == 400.0


def test_parse_and_filter_dexscreener_pairs():
    c = parse_pair(PAIR)
    assert c["mint"] == MINT and c["mcap_usd"] == 120_000 and c["txns_m5"] == 65
    assert c["socials"] == {"twitter": "x.com/estcoin", "website": "estcoin.io"}
    d = DexDiscovery(cfg())
    assert d.accept(c)
    assert not d.accept(dict(c, mcap_usd=20_000))  # below $25k
    assert not d.accept(dict(c, txns_m5=5))  # not active
    assert parse_pair(dict(PAIR, dexId="raydium")) is None  # only pump.fun coins
    assert parse_pair(dict(PAIR, baseToken={"address": "So1111"})) is None


def _bot(tmp_path, **kw):
    c = cfg(tmp_path, **{**TACTICS["est_momentum"], "scalp_eval_interval_sec": 0, **kw})
    market = MarketData(c, offline=True)
    market.sol.usd = 100.0
    now = [2_000_000.0]
    bot = ScalperBot(c, PaperExecutor(c), feed=FakeFeed(), clock=lambda: now[0], socials=FakeSocials(GOOD_META),
                     market=market)
    return bot, market, now


async def _discover_and_pump(bot, market, now, n=40):
    market.discovery.set_items([parse_pair(PAIR)])
    await bot.tick()
    assert MINT in bot.tokens and bot.tokens[MINT].partial
    sol_pool, tok_pool = 135.0, 118_000_000.0
    for i in range(n):
        now[0] += 1.2
        side = "sell" if i % 6 == 5 else "buy"
        sol_pool *= 1.002 if side == "buy" else 0.999
        await bot.handle(amm_msg(f"w{i % 25}", side, 0.6, 50_000, sol_pool, tok_pool, f"t{i}", mcap_sol=1200))
    await bot.drain()


def test_established_coin_needs_holder_check_then_buys(tmp_path):
    bot, market, now = _bot(tmp_path)

    async def go():
        await _discover_and_pump(bot, market, now)
        assert not bot.positions.open  # holders unknown -> no buy
        assert any("holders not checked" in k for k in bot.skip_reasons)
        market.holders.set(MINT, now[0], top10=22.0, mx=4.0)  # healthy distribution
        await bot.handle(amm_msg("w99", "buy", 0.6, 50_000, 150.0, 118_000_000.0, "go", mcap_sol=1200))
        await bot.drain()
        assert MINT in bot.positions.open

    asyncio.run(go())


def test_established_coin_with_whale_holders_is_skipped(tmp_path):
    bot, market, now = _bot(tmp_path)

    async def go():
        market.holders.set(MINT, now[0], top10=60.0, mx=20.0)
        await _discover_and_pump(bot, market, now)
        assert not bot.positions.open
        assert any("top-#" in k or "hold" in k for k in bot.skip_reasons)

    asyncio.run(go())


def test_holder_check_can_be_turned_off(tmp_path):
    bot, market, now = _bot(tmp_path, require_holder_check=False)

    async def go():
        await _discover_and_pump(bot, market, now)
        assert MINT in bot.positions.open

    asyncio.run(go())


def test_usd_market_cap_band():
    c = cfg(scalp_min_market_cap_usd=25_000)
    m = MarketData(c, offline=True)
    m.sol.usd = 100.0
    an = PotentialAnalyzer(c, m)
    from pumpbot.tokens import TokenState

    t = TokenState("M", "dev", 0)
    t.market_cap_sol = 200  # $20k
    assert "mcap outside band" in an.assess(t, 100).reasons
    t.market_cap_sol = 300  # $30k
    assert "mcap outside band" not in an.assess(t, 100).reasons


def test_coins_that_grew_past_25k_are_kept_longer(tmp_path):
    bot, market, now = _bot(tmp_path)

    async def go():
        now[0] = 0.0
        await bot.handle(msg("DEV", "BIG", "create", 0.5, 20_000_000, 3e-8, "c1", uri="u", mcap=300))  # $30k
        await bot.handle(msg("DEV", "SMALL", "create", 0.5, 20_000_000, 3e-8, "c2", uri="u", mcap=40))
        await bot.drain()
        now[0] = 3 * 3600  # far past the new-coin watch time
        bot.tokens["BIG"].last_trade_at = now[0] - 10  # still trading
        await bot._prune_tokens(now[0])
        assert "BIG" in bot.tokens and "SMALL" not in bot.tokens

    asyncio.run(go())


def test_est_tactics_exist():
    est = [n for n in TACTICS if n.startswith("est_")]
    assert len(est) >= 5
    assert all(TACTICS[n]["scalp_min_market_cap_usd"] == 25_000 for n in est)


def test_hidden_supply_blocks_dev_held_pool_launch(tmp_path):
    """Regression: a coin launched straight into a PumpSwap pool holding only 12% of
    supply; wallets that never traded dumped 33% and every tactic lost -96%."""
    bot, market, now = _bot(tmp_path, require_holder_check=False)

    async def go():
        now[0] = 0.0
        create = amm_msg("DEV", "create", 0.0, 0.0, 150.0, 120_000_000, "c", mcap_sol=1250)
        create["initialBuy"] = 0
        await bot.handle(create)
        await bot.drain()
        sol_pool, tok_pool = 150.0, 120_000_000.0  # 880M tokens outside the pool, nobody seen buying them
        for i in range(60):
            now[0] = 40 + i * 1.0
            side = "sell" if i % 6 == 5 else "buy"
            sol_pool *= 1.002 if side == "buy" else 0.999
            tok_pool *= 0.9995 if side == "buy" else 1.0003
            await bot.handle(amm_msg(f"w{i % 25}", side, 0.6, 50_000, sol_pool, tok_pool, f"h{i}", mcap_sol=1250))
        await bot.drain()
        t = bot.tokens[MINT]
        assert t.hidden_supply_pct > 80
        assert not bot.positions.open
        assert any("never bought" in k for k in bot.skip_reasons)

    asyncio.run(go())
