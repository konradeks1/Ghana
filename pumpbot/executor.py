"""Order execution: paper simulation, or live via PumpPortal's local-transaction API.

Live mode builds the transaction on PumpPortal, signs it locally with your key
(the key never leaves this machine) and submits it to your Solana RPC.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)

PAPER_SLIPPAGE_PCT = 2.0  # realistic average fill slippage for the simulation


@dataclass
class Fill:
    ok: bool
    sol: float  # SOL spent (buy) or received (sell)
    tokens: float
    price: float
    signature: str = ""
    error: str = ""


class PaperExecutor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.balance_sol = cfg.paper_balance_sol

    async def buy(self, mint: str, sol: float, price: float) -> Fill:
        fill_price = price * (1 + PAPER_SLIPPAGE_PCT / 100)
        tokens = sol * (1 - self.cfg.fee_pct / 100) / fill_price
        self.balance_sol -= sol + self.cfg.priority_fee_sol
        return Fill(True, sol, tokens, fill_price, "paper")

    async def sell(self, mint: str, tokens: float, price: float) -> Fill:
        fill_price = price * (1 - PAPER_SLIPPAGE_PCT / 100)
        sol = tokens * fill_price * (1 - self.cfg.fee_pct / 100) - self.cfg.priority_fee_sol
        self.balance_sol += sol
        return Fill(True, sol, tokens, fill_price, "paper")

    async def balance(self) -> float:
        return self.balance_sol

    async def close(self) -> None:
        pass


class PumpPortalExecutor:
    def __init__(self, cfg: Config):
        import httpx
        from solders.keypair import Keypair

        if not cfg.private_key:
            raise ValueError("PRIVATE_KEY is required when DRY_RUN=false")
        self.cfg = cfg
        self.keypair = Keypair.from_base58_string(cfg.private_key)
        self.pubkey = str(self.keypair.pubkey())
        self.http = httpx.AsyncClient(timeout=20)
        log.warning("LIVE trading enabled for wallet %s", self.pubkey)

    async def _trade(self, action: str, mint: str, amount: float | str, in_sol: bool) -> tuple[str, str]:
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
        rpc = await self.http.post(
            self.cfg.rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    base64.b64encode(bytes(signed)).decode(),
                    {"encoding": "base64", "skipPreflight": True, "maxRetries": 3},
                ],
            },
        )
        body = rpc.json()
        if "error" in body:
            return "", f"rpc: {body['error']}"
        return body.get("result", ""), ""

    async def buy(self, mint: str, sol: float, price: float) -> Fill:
        sig, err = await self._trade("buy", mint, sol, in_sol=True)
        if err:
            return Fill(False, 0, 0, price, error=err)
        tokens = sol * (1 - self.cfg.fee_pct / 100) / price  # estimate; sell uses 100%
        return Fill(True, sol, tokens, price, sig)

    async def sell(self, mint: str, tokens: float, price: float) -> Fill:
        sig, err = await self._trade("sell", mint, "100%", in_sol=False)
        if err:
            return Fill(False, 0, tokens, price, error=err)
        sol = tokens * price * (1 - self.cfg.fee_pct / 100)  # estimate from last price
        return Fill(True, sol, tokens, price, sig)

    async def balance(self) -> float:
        r = await self.http.post(
            self.cfg.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [self.pubkey]},
        )
        return r.json()["result"]["value"] / 1e9

    async def close(self) -> None:
        await self.http.aclose()


def make_executor(cfg: Config):
    return PaperExecutor(cfg) if cfg.dry_run else PumpPortalExecutor(cfg)
