import asyncio
import base64
import struct

from solders.keypair import Keypair

from pumpbot.models import TradeEvent
from pumpbot.solfeed import D_BUY, D_CREATE, D_TRADE, SolanaLogFeed, decode_amm, decode_create, decode_curve_trade

MINT = Keypair().pubkey()
USER = Keypair().pubkey()
POOL = Keypair().pubkey()


def curve_trade_bytes(is_buy=True):
    return (D_TRADE + bytes(MINT) + struct.pack("<QQ", 1_100_000_000, 12_439_270_568_036) + bytes([is_buy])
            + bytes(USER) + struct.pack("<qQQQQ", 1_700_000_000, 53_905_899_933, 597_151_706_774_902, 0, 0))


def s(x):
    return struct.pack("<I", len(x)) + x.encode()


def test_decode_curve_trade():
    m = decode_curve_trade(curve_trade_bytes(), "sig:0")
    assert m["mint"] == str(MINT) and m["traderPublicKey"] == str(USER) and m["txType"] == "buy"
    assert m["solAmount"] == 1.1 and abs(m["vSolInBondingCurve"] - 53.905899933) < 1e-9
    e = TradeEvent.from_pumpportal(m, now=1)
    assert abs(e.price - 53.905899933 / 597_151_706.774902) < 1e-15


def test_decode_create():
    b = D_CREATE + s("Good Coin") + s("GOOD") + s("https://ipfs.io/ipfs/x") + bytes(MINT) + bytes(Keypair().pubkey()) \
        + bytes(USER) + bytes(USER) + struct.pack("<qQQ", 1, 1_073_000_000_000_000, 30_000_000_000)
    m = decode_create(b, "sig:1")
    assert (m["name"], m["symbol"], m["mint"], m["txType"]) == ("Good Coin", "GOOD", str(MINT), "create")
    assert abs(m["marketCapSol"] - 30 / 1_073_000_000 * 1e9) < 1e-6


def test_decode_amm_and_feed_filtering():
    fields = struct.pack("<q13Q", 1, 492_092_000_000, 0, 0, 0, 78_653_137_000_000, 273_670_000_000, 1_818_400_000,
                         0, 0, 0, 0, 0, 0)
    b = D_BUY + fields + bytes(POOL) + bytes(USER)
    pool, user, f = decode_amm(b, True)
    assert pool == str(POOL) and user == str(USER) and f["txType"] == "buy" and f["pool"] == "pump-amm"
    assert abs(f["solInPool"] - 273.67) < 1e-9 and abs(f["tokenAmount"] - 492_092) < 1e-6

    got = []

    async def handler(msg):
        got.append(msg)

    feed = SolanaLogFeed("wss://x", "https://x", handler)
    feed._pools[str(POOL)] = str(MINT)

    def logs(*events):
        return {"signature": "S", "err": None,
                "logs": [f"Program data: {base64.b64encode(e).decode()}" for e in events]}

    async def go():
        await feed._on_logs(logs(curve_trade_bytes(), b))
        assert got == []  # not subscribed -> nothing passed on (like PumpPortal)
        await feed.subscribe_tokens([str(MINT)])
        await feed._on_logs(logs(curve_trade_bytes(False), b))
        assert [m["pool"] for m in got] == ["pump", "pump-amm"]
        assert got[0]["signature"] != got[1]["signature"]  # unique per event, not per transaction
        await feed._on_logs({**logs(curve_trade_bytes()), "err": {"InstructionError": [0, "x"]}})
        assert len(got) == 2  # failed transactions are ignored

    asyncio.run(go())
