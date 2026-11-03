"""Market data for trading coins that are already established ($25k+ market cap).

* SolPrice      SOL/USD (Jupiter, CoinGecko as fallback) to turn SOL market caps into dollars
* DexDiscovery  finds pump.fun coins that are already trading well, via DexScreener's
                free API: latest token profiles, boosts and community takeovers,
                then filters on market cap, volume, activity and liquidity
* HolderChecker top-holder concentration from the Solana RPC (getTokenLargestAccounts).
                Coins discovered mid-life have no launch history, so this replaces
                the trade-based holder checks for them.

All three are shared between tactics (one fetch serves every tactic) and can
run offline from a recording (``offline=True``) so replays are exact.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import Config
from .socials import classify

log = logging.getLogger("pumpbot.market")

DEX = "https://api.dexscreener.com"
WSOL = "So11111111111111111111111111111111111111112"


class _Http:
    def __init__(self, http=None):
        self._http = http

    async def client(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=10, follow_redirects=True)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


class SolPrice(_Http):
    def __init__(self, cfg: Config, http=None, offline: bool = False, recorder=None):
        super().__init__(http)
        self.usd = cfg.sol_price_usd
        self.ttl = 300.0
        self.offline = offline
        self.recorder = recorder
        self._fetched = -1e18
        self._lock = asyncio.Lock()

    async def refresh(self, now: float) -> float:
        if self.offline or now - self._fetched < self.ttl:
            return self.usd
        async with self._lock:
            if now - self._fetched < self.ttl:
                return self.usd
            self._fetched = now
            http = await self.client()
            for url, pick in (
                (f"https://lite-api.jup.ag/price/v3?ids={WSOL}", lambda d: d[WSOL]["usdPrice"]),
                ("https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                 lambda d: d["solana"]["usd"]),
            ):
                try:
                    r = await http.get(url)
                    price = float(pick(r.json()))
                    if price > 0:
                        self.usd = price
                        if self.recorder:
                            self.recorder({"sol_usd": price})
                        break
                except Exception as e:
                    log.debug("SOL price from %s failed: %s", url, e)
        return self.usd

    def mcap_usd(self, mcap_sol: float) -> float:
        return mcap_sol * self.usd if self.usd > 0 else 0.0


def parse_pair(p: dict[str, Any]) -> dict[str, Any] | None:
    """DexScreener pair -> what the bot needs, or None if not a pump.fun coin."""
    base = p.get("baseToken") or {}
    mint = base.get("address", "")
    if p.get("chainId") != "solana" or not mint.endswith("pump"):
        return None
    if p.get("dexId") not in ("pumpfun", "pumpswap"):
        return None
    info = p.get("info") or {}
    socials: dict[str, str] = {}
    urls = [s.get("url", "") for s in info.get("socials") or []] + [w.get("url", "") for w in info.get("websites") or []]
    for u in urls:
        res = classify(u) if u else None
        if res and res[0] not in socials:
            socials[res[0]] = res[1]
    txns = p.get("txns") or {}
    vol = p.get("volume") or {}
    return {
        "mint": mint,
        "symbol": str(base.get("symbol", ""))[:16],
        "name": str(base.get("name", ""))[:40],
        "dex": p.get("dexId"),
        "pair": p.get("pairAddress", ""),
        "mcap_usd": float(p.get("marketCap") or p.get("fdv") or 0),
        "liquidity_usd": float((p.get("liquidity") or {}).get("usd") or 0),
        "volume_h1": float(vol.get("h1") or 0),
        "txns_m5": int((txns.get("m5") or {}).get("buys", 0)) + int((txns.get("m5") or {}).get("sells", 0)),
        "created_at": (p.get("pairCreatedAt") or 0) / 1000,
        "socials": socials,
    }


class DexDiscovery(_Http):
    SOURCES = ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1",
               "/community-takeovers/latest/v1")

    def __init__(self, cfg: Config, http=None, offline: bool = False, recorder=None):
        super().__init__(http)
        self.cfg = cfg
        self.offline = offline
        self.recorder = recorder
        self.items: list[dict[str, Any]] = []
        self.version = 0
        self._polled = -1e18
        self._lock = asyncio.Lock()

    def accept(self, c: dict[str, Any]) -> bool:
        cfg = self.cfg
        if c["mcap_usd"] < cfg.discovery_min_mcap_usd:
            return False
        if cfg.discovery_max_mcap_usd and c["mcap_usd"] > cfg.discovery_max_mcap_usd:
            return False
        if c["volume_h1"] < cfg.discovery_min_h1_volume_usd or c["txns_m5"] < cfg.discovery_min_m5_txns:
            return False
        if c["dex"] == "pumpswap" and c["liquidity_usd"] < cfg.discovery_min_liquidity_usd:
            return False
        return True

    def set_items(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.version += 1

    async def poll(self, now: float) -> None:
        if self.offline or now - self._polled < self.cfg.discovery_interval_sec:
            return
        async with self._lock:
            if now - self._polled < self.cfg.discovery_interval_sec:
                return
            self._polled = now
            http = await self.client()
            mints: list[str] = []
            for path in self.SOURCES:
                try:
                    for row in (await http.get(DEX + path)).json() or []:
                        addr = row.get("tokenAddress", "")
                        if row.get("chainId") == "solana" and addr.endswith("pump") and addr not in mints:
                            mints.append(addr)
                except Exception as e:
                    log.debug("dexscreener %s failed: %s", path, e)
            found: dict[str, dict[str, Any]] = {}
            for i in range(0, len(mints), 30):
                try:
                    pairs = (await http.get(f"{DEX}/tokens/v1/solana/{','.join(mints[i:i + 30])}")).json() or []
                except Exception as e:
                    log.debug("dexscreener tokens failed: %s", e)
                    continue
                for p in pairs:
                    c = parse_pair(p)
                    if c and self.accept(c):
                        # keep the most liquid pair per coin
                        if c["mint"] not in found or c["liquidity_usd"] > found[c["mint"]]["liquidity_usd"]:
                            found[c["mint"]] = c
            items = sorted(found.values(), key=lambda c: -c["txns_m5"])[: self.cfg.discovery_max_coins]
            self.set_items(items)
            if self.recorder:
                self.recorder({"discovery": items})
            log.info("discovery: %d established coins from %d candidates", len(items), len(mints))


class HolderChecker(_Http):
    """Top-holder concentration via RPC. Pool / bonding-curve accounts are excluded."""

    def __init__(self, cfg: Config, http=None, offline: bool = False, recorder=None):
        super().__init__(http)
        self.cfg = cfg
        self.offline = offline
        self.recorder = recorder
        self.cache: dict[str, tuple[float, float | None, float | None, str]] = {}  # mint -> (ts, top10, max, err)
        self._inflight: dict[str, asyncio.Future] = {}
        self._warned = False

    def get(self, mint: str, now: float) -> tuple[float | None, float | None, str] | None:
        hit = self.cache.get(mint)
        if hit is None or (not self.offline and now - hit[0] > self.cfg.holder_check_ttl_sec):
            return None
        return hit[1], hit[2], hit[3]

    def set(self, mint: str, now: float, top10: float | None, mx: float | None, err: str = "") -> None:
        self.cache[mint] = (now, top10, mx, err)

    async def check(self, mint: str, exclude_owners: set[str], now: float) -> None:
        if self.offline or mint in self._inflight:
            return
        fut = self._inflight[mint] = asyncio.ensure_future(self._check(mint, exclude_owners, now))
        try:
            await fut
        finally:
            self._inflight.pop(mint, None)

    async def _rpc(self, method: str, params: list):
        http = await self.client()
        r = await http.post(self.cfg.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        body = r.json()
        if "error" in body:
            raise RuntimeError(body["error"].get("message", str(body["error"])))
        return body["result"]

    async def _check(self, mint: str, exclude_owners: set[str], now: float) -> None:
        try:
            largest = (await self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]))["value"]
            accts = [a["address"] for a in largest]
            infos = (await self._rpc("getMultipleAccounts", [accts, {"encoding": "jsonParsed"}]))["value"]
            holders = []
            for a, info in zip(largest, infos):
                owner = (((info or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner", "")
                if owner in exclude_owners:
                    continue  # the bonding curve / AMM pool, not a holder
                holders.append(float(a.get("uiAmountString") or a.get("uiAmount") or 0))
            holders.sort(reverse=True)
            supply = 1_000_000_000.0
            top10 = sum(holders[:10]) / supply * 100
            mx = (holders[0] / supply * 100) if holders else 0.0
            self.set(mint, now, top10, mx)
            if self.recorder:
                self.recorder({"holders": {mint: [top10, mx, ""]}})
        except Exception as e:
            err = f"holder check failed: {e}"[:120]
            if not self._warned:
                self._warned = True
                log.warning("%s — set RPC_URL to an RPC that allows getTokenLargestAccounts (e.g. Helius)", err)
            self.set(mint, now, None, None, err)
            if self.recorder:
                self.recorder({"holders": {mint: [None, None, err]}})


class MarketData:
    """Bundle shared by all tactics."""

    def __init__(self, cfg: Config, offline: bool = False, recorder=None):
        self.sol = SolPrice(cfg, offline=offline, recorder=recorder)
        self.discovery = DexDiscovery(cfg, offline=offline, recorder=recorder)
        self.holders = HolderChecker(cfg, offline=offline, recorder=recorder)

    def apply_record(self, rec: dict[str, Any], now: float) -> None:
        if "sol_usd" in rec:
            self.sol.usd = rec["sol_usd"]
        if "discovery" in rec:
            self.discovery.set_items(rec["discovery"])
        for mint, (top10, mx, err) in (rec.get("holders") or {}).items():
            self.holders.set(mint, now, top10, mx, err)

    async def close(self) -> None:
        for part in (self.sol, self.discovery, self.holders):
            await part.close()


def now() -> float:
    return time.time()
