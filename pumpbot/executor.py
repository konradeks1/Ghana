"""Order execution: paper simulation, or live via PumpPortal's local-transaction API.

Live mode builds the transaction on PumpPortal, signs it locally with your key
(the key never leaves this machine), submits it to your Solana RPC, waits for
on-chain confirmation, and reads the *actual* SOL spent/received and tokens
held from the chain. Nothing is assumed filled until the chain says so.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)

LAMPORTS = 1_000_000_000


@dataclass
class Fill:
    ok: bool
    sol: float  # SOL spent (buy) or received (sell)
    tokens: float
    price: float
    signature: str = ""
    error: str = ""


class PaperExecutor:
    """Simulated fills that behave like the real thing.

    * latency: the fill happens ``paper_latency_sec`` after the decision, at
      whatever the market price is *then* (read from the live feed)
    * slippage limit: if price moved against us by more than ``slippage_pct``
      during that time, the order fails like a real one would
    * price impact: computed from the coin's bonding-curve liquidity (a 0.1
      SOL buy into a curve holding 40 SOL moves the average fill ~0.25%),
      plus ``paper_slippage_pct`` for other traders in the same block
    * fees: pump.fun fee on each side plus the priority fee
    """

    def __init__(self, cfg: Config, price_fn: Callable[[str], float | None] | None = None, sleep=asyncio.sleep):
        self.cfg = cfg
        self.balance_sol = cfg.paper_balance_sol
        self.price_fn = price_fn
        self.reserve_fn: Callable[[str], float | None] | None = None
        self._sleep = sleep

    def _impact_pct(self, mint: str, sol: float) -> float:
        reserve = self.reserve_fn(mint) if self.reserve_fn else None
        curve = sol / reserve * 100 if reserve and reserve > 0 else 1.0
        return curve + self.cfg.paper_slippage_pct

    async def _market_price(self, mint: str, signal_price: float) -> float:
        if self.cfg.paper_latency_sec > 0:
            await self._sleep(self.cfg.paper_latency_sec)
        p = self.price_fn(mint) if self.price_fn else None
        return p if p and p > 0 else signal_price

    async def buy(self, mint: str, sol: float, price: float) -> Fill:
        market = await self._market_price(mint, price)
        moved = (market / price - 1) * 100
        if moved > self.cfg.slippage_pct:
            return Fill(False, 0, 0, market, error=f"slippage exceeded: price +{moved:.1f}% during latency")
        fill_price = market * (1 + self._impact_pct(mint, sol) / 100)
        tokens = sol * (1 - self.cfg.fee_pct / 100) / fill_price
        cost = sol + self.cfg.priority_fee_sol
        self.balance_sol -= cost
        return Fill(True, cost, tokens, cost / tokens, "paper")

    async def sell(self, mint: str, tokens: float, price: float) -> Fill:
        market = await self._market_price(mint, price)
        moved = (1 - market / price) * 100
        if moved > self.cfg.slippage_pct:
            return Fill(False, 0, tokens, market, error=f"slippage exceeded: price -{moved:.1f}% during latency")
        fill_price = market * (1 - self._impact_pct(mint, tokens * market) / 100)
        sol = tokens * fill_price * (1 - self.cfg.fee_pct / 100) - self.cfg.priority_fee_sol
        self.balance_sol += sol
        return Fill(True, sol, tokens, fill_price, "paper")

    async def balance(self) -> float:
        return self.balance_sol

    async def close(self) -> None:
        pass


class PumpPortalExecutor:
    def __init__(self, cfg: Config, sleep=asyncio.sleep):
        import httpx
        from solders.keypair import Keypair

        if not cfg.private_key:
            raise ValueError("PRIVATE_KEY is required when DRY_RUN=false")
        self.cfg = cfg
        self.keypair = Keypair.from_base58_string(cfg.private_key)
        self.pubkey = str(self.keypair.pubkey())
        self.http = httpx.AsyncClient(timeout=20)
        self._sleep = sleep
        log.warning("LIVE trading enabled for wallet %s", self.pubkey)

    # ---------------------------------------------------------------- RPC
    async def _rpc(self, method: str, params: list):
        r = await self.http.post(
            self.cfg.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error']}")
        return body.get("result")

    async def token_balance(self, mint: str) -> float:
        res = await self._rpc(
            "getTokenAccountsByOwner",
            [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        total = 0.0
        for acc in (res or {}).get("value", []):
            amt = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amt.get("uiAmountString") or amt.get("uiAmount") or 0)
        return total

    async def _confirm(self, sig: str) -> tuple[bool | None, str]:
        """(True, "") confirmed ok, (False, err) failed on-chain, (None, "") timed out."""
        waited = 0.0
        while waited < self.cfg.confirm_timeout_sec:
            res = await self._rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])
            st = (res or {}).get("value", [None])[0]
            if st:
                if st.get("err"):
                    return False, str(st["err"])
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True, ""
            await self._sleep(1.0)
            waited += 1.0
        return None, ""

    async def _sol_delta(self, sig: str) -> float | None:
        """Exact SOL change of our wallet (fee payer) in this transaction, fees included."""
        for _ in range(5):
            tx = await self._rpc(
                "getTransaction",
                [sig, {"encoding": "json", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
            )
            if tx and tx.get("meta"):
                meta = tx["meta"]
                return (meta["postBalances"][0] - meta["preBalances"][0]) / LAMPORTS
            await self._sleep(1.0)
        return None

    async def _send(self, action: str, mint: str, amount: float | str, in_sol: bool) -> tuple[str, str]:
        from solders.transaction import VersionedTransaction

        resp = await self.http.post(
            self.cfg.trade_url,
            data={
                "publicKey": self.pubkey,
                "action": action,
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true" if in_sol else "false",
                "slippage": self.cfg.slippage_pct,
                "priorityFee": self.cfg.priority_fee_sol,
                "pool": self.cfg.pool,
            },
        )
        if resp.status_code != 200:
            return "", f"trade api {resp.status_code}: {resp.text[:200]}"
        unsigned = VersionedTransaction.from_bytes(resp.content)
        signed = VersionedTransaction(unsigned.message, [self.keypair])
        try:
            sig = await self._rpc(
                "sendTransaction",
                [
                    base64.b64encode(bytes(signed)).decode(),
                    {"encoding": "base64", "skipPreflight": True, "maxRetries": 3},
                ],
            )
        except RuntimeError as e:
            return "", str(e)
        return sig or "", ""

    # ------------------------------------------------------------- orders
    async def buy(self, mint: str, sol: float, price: float) -> Fill:
        try:
            before = await self.token_balance(mint)
            sig, err = await self._send("buy", mint, sol, in_sol=True)
            if err:
                return Fill(False, 0, 0, price, error=err)
            ok, err = await self._confirm(sig)
            if ok is False:
                return Fill(False, 0, 0, price, sig, f"failed on-chain: {err}")
            got = await self.token_balance(mint) - before
            if got <= 0:
                if ok is None:
                    log.warning("buy %s not confirmed in time; it may still land — check the wallet", sig)
                return Fill(False, 0, 0, price, sig, "no tokens received")
            delta = await self._sol_delta(sig)
            spent = -delta if delta is not None else sol + self.cfg.priority_fee_sol
            return Fill(True, spent, got, spent / got, sig)
        except Exception as e:
            return Fill(False, 0, 0, price, error=f"{type(e).__name__}: {e}")

    async def sell(self, mint: str, tokens: float, price: float) -> Fill:
        try:
            before = await self.token_balance(mint)
            if before <= 0:
                return Fill(False, 0, 0, price, error="no tokens in wallet")
            sig, err = await self._send("sell", mint, "100%", in_sol=False)
            if err:
                return Fill(False, 0, before, price, error=err)
            ok, err = await self._confirm(sig)
            if ok is False:
                return Fill(False, 0, before, price, sig, f"failed on-chain: {err}")
            after = await self.token_balance(mint)
            if after > before * 0.01:
                return Fill(False, 0, after, price, sig, "tokens still in wallet")
            delta = await self._sol_delta(sig)
            received = delta if delta is not None else before * price * (1 - self.cfg.fee_pct / 100)
            return Fill(True, received, before, received / before, sig)
        except Exception as e:
            return Fill(False, 0, tokens, price, error=f"{type(e).__name__}: {e}")

    async def balance(self) -> float:
        res = await self._rpc("getBalance", [self.pubkey, {"commitment": "confirmed"}])
        return res["value"] / LAMPORTS

    async def close(self) -> None:
        await self.http.aclose()


def make_executor(cfg: Config):
    return PaperExecutor(cfg) if cfg.dry_run else PumpPortalExecutor(cfg)
