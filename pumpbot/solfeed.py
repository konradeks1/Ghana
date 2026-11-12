"""Free live data: Pump.fun and PumpSwap trades straight from Solana's program logs.

No PumpPortal API key and no fees. A public (or your own) Solana RPC websocket
streams the logs of the Pump.fun bonding-curve program and the PumpSwap AMM.
The Anchor events in those logs are decoded into the same message shape
PumpPortal sends, so the rest of the bot works unchanged.

    curve: CreateEvent, TradeEvent        -> txType create / buy / sell, pool "pump"
    AMM:   BuyEvent, SellEvent            -> txType buy / sell, pool "pump-amm"

AMM events name the pool, not the coin, so pools are resolved to their coin
(base mint) with getMultipleAccounts and cached. Only pump.fun coins quoted in
SOL are kept.

Like PumpPortal, only coins the bot subscribed to are passed on (plus every
new coin's create event).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
from collections.abc import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
SUPPLY = 1_000_000_000.0


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


D_CREATE, D_TRADE = _disc("CreateEvent"), _disc("TradeEvent")
D_BUY, D_SELL = _disc("BuyEvent"), _disc("SellEvent")


def _pk(b: bytes, o: int) -> str:
    from solders.pubkey import Pubkey

    return str(Pubkey.from_bytes(b[o:o + 32]))


def _string(b: bytes, o: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", b, o)
    return b[o + 4:o + 4 + n].decode("utf-8", "replace"), o + 4 + n


def decode_curve_trade(b: bytes, sig: str) -> dict:
    o = 8
    mint = _pk(b, o); o += 32
    sol, tok = struct.unpack_from("<QQ", b, o); o += 16
    is_buy = b[o] == 1; o += 1
    user = _pk(b, o); o += 32
    _ts, vsol, vtok = struct.unpack_from("<qQQ", b, o)
    vsol, vtok = vsol / 1e9, vtok / 1e6
    return {
        "signature": sig, "mint": mint, "traderPublicKey": user, "txType": "buy" if is_buy else "sell",
        "solAmount": sol / 1e9, "tokenAmount": tok / 1e6, "vSolInBondingCurve": vsol,
        "vTokensInBondingCurve": vtok, "marketCapSol": vsol / vtok * SUPPLY if vtok else 0.0, "pool": "pump",
    }


def decode_create(b: bytes, sig: str) -> dict:
    o = 8
    name, o = _string(b, o)
    symbol, o = _string(b, o)
    uri, o = _string(b, o)
    mint = _pk(b, o); o += 32
    o += 32  # bonding curve
    user = _pk(b, o); o += 32
    creator = user
    vsol, vtok = 30.0, 1_073_000_000.0  # pump.fun's fixed starting curve
    if len(b) >= o + 32 + 8 + 16:
        creator = _pk(b, o); o += 32
        o += 8  # timestamp
        vtok_raw, vsol_raw = struct.unpack_from("<QQ", b, o)
        if vtok_raw and vsol_raw:
            vtok, vsol = vtok_raw / 1e6, vsol_raw / 1e9
    return {
        "signature": sig, "mint": mint, "traderPublicKey": creator, "txType": "create", "name": name,
        "symbol": symbol, "uri": uri, "solAmount": 0.0, "initialBuy": 0.0, "vSolInBondingCurve": vsol,
        "vTokensInBondingCurve": vtok, "marketCapSol": vsol / vtok * SUPPLY, "pool": "pump",
    }


def decode_amm(b: bytes, is_buy: bool) -> tuple[str, str, dict]:
    """-> (pool, user, fields). Both events start with 14 u64/i64 then pool, user."""
    f = struct.unpack_from("<q13Q", b, 8)
    base_amt, base_res, quote_res, quote_amt = f[1], f[5], f[6], f[7]
    return _pk(b, 120), _pk(b, 152), {
        "txType": "buy" if is_buy else "sell", "tokenAmount": base_amt / 1e6, "solAmount": quote_amt / 1e9,
        "tokensInPool": base_res / 1e6, "solInPool": quote_res / 1e9, "pool": "pump-amm",
    }


Handler = Callable[[dict], Awaitable[None]]


class SolanaLogFeed:
    def __init__(self, ws_url: str, rpc_url: str, handler: Handler, http=None):
        self.ws_url = ws_url
        self.rpc_url = rpc_url
        self.handler = handler
        self.tokens: set[str] = set()
        self.accounts: set[str] = set()
        self.trade_data_blocked = False
        self._stop = asyncio.Event()
        self._http = http
        self._pools: dict[str, str | None] = {}  # pool -> pump.fun mint quoted in SOL (None = ignore)
        self._pending: dict[str, list[tuple[str, str, dict]]] = {}
        self._resolver: asyncio.Task | None = None
        self.events = 0

    # PumpPortal-compatible subscription API
    async def subscribe_tokens(self, mints: Iterable[str]) -> None:
        self.tokens |= set(mints)

    async def unsubscribe_tokens(self, mints: Iterable[str]) -> None:
        self.tokens -= set(mints)

    async def set_accounts(self, wallets: Iterable[str]) -> None:
        self.accounts = set(wallets)

    def stop(self) -> None:
        self._stop.set()

    def _wanted(self, msg: dict) -> bool:
        return msg["mint"] in self.tokens or msg["traderPublicKey"] in self.accounts

    async def _emit(self, msg: dict) -> None:
        self.events += 1
        try:
            await self.handler(msg)
        except Exception:
            log.exception("handler error")

    # ------------------------------------------------------------ pools
    async def _resolve_loop(self) -> None:
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        while not self._stop.is_set():
            await asyncio.sleep(0.4)
            pools = list(self._pending)[:100]
            if not pools:
                continue
            try:
                r = (await self._http.post(self.rpc_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "getMultipleAccounts",
                    "params": [pools, {"encoding": "base64"}]})).json()
                accounts = r["result"]["value"]
            except Exception as e:
                log.debug("pool lookup failed: %s", e)
                await asyncio.sleep(2)
                continue
            for pool, acc in zip(pools, accounts):
                mint = None
                if acc:
                    data = base64.b64decode(acc["data"][0])
                    if len(data) >= 107:
                        base, quote = _pk(data, 43), _pk(data, 75)
                        if base.endswith("pump") and quote == WSOL:
                            mint = base
                self._pools[pool] = mint
                for sig, user, fields in self._pending.pop(pool, []):
                    if mint:
                        await self._emit_amm(mint, sig, user, fields)

    async def _emit_amm(self, mint: str, sig: str, user: str, fields: dict) -> None:
        msg = {"signature": sig, "mint": mint, "traderPublicKey": user, **fields}
        if fields["tokensInPool"] > 0:
            msg["marketCapSol"] = fields["solInPool"] / fields["tokensInPool"] * SUPPLY
        if self._wanted(msg):
            await self._emit(msg)

    # ------------------------------------------------------------- logs
    async def _on_logs(self, value: dict) -> None:
        if value.get("err"):
            return
        sig = value.get("signature", "")
        for i, line in enumerate(value.get("logs") or []):
            if not line.startswith("Program data: "):
                continue
            try:
                b = base64.b64decode(line[14:])
            except Exception:
                continue
            d = b[:8]
            uid = f"{sig}:{i}"  # one transaction can carry several events
            try:
                if d == D_TRADE:
                    msg = decode_curve_trade(b, uid)
                    if self._wanted(msg):
                        await self._emit(msg)
                elif d == D_CREATE:
                    await self._emit(decode_create(b, uid))
                elif d in (D_BUY, D_SELL):
                    pool, user, fields = decode_amm(b, d == D_BUY)
                    if pool in self._pools:
                        mint = self._pools[pool]
                        if mint:
                            await self._emit_amm(mint, uid, user, fields)
                    elif len(self._pending) < 5000:
                        self._pending.setdefault(pool, []).append((uid, user, fields))
            except (struct.error, ValueError, IndexError) as e:
                log.debug("undecodable event: %s", e)

    async def run(self) -> None:
        import websockets

        self._resolver = asyncio.create_task(self._resolve_loop())
        backoff = 1
        try:
            while not self._stop.is_set():
                try:
                    async with websockets.connect(self.ws_url, ping_interval=20, max_size=2**24) as ws:
                        for i, program in enumerate((PUMP_PROGRAM, AMM_PROGRAM)):
                            await ws.send(json.dumps({
                                "jsonrpc": "2.0", "id": i + 1, "method": "logsSubscribe",
                                "params": [{"mentions": [program]}, {"commitment": "confirmed"}]}))
                        log.info("connected to Solana logs at %s (pump.fun + PumpSwap)", self.ws_url.split("?")[0])
                        backoff = 1
                        async for raw in ws:
                            msg = json.loads(raw)
                            if "params" in msg:
                                await self._on_logs(msg["params"]["result"]["value"])
                            elif "error" in msg:
                                log.warning("solana rpc: %s", msg["error"])
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("solana feed disconnected (%s); retrying in %ss", e, backoff)
                if not self._stop.is_set():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            self._resolver.cancel()
            if self._http is not None:
                await self._http.aclose()
