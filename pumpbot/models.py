"""Normalised trade events parsed from the PumpPortal data stream."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TradeEvent:
    signature: str
    mint: str
    trader: str
    side: str  # "buy" | "sell" | "create"
    sol_amount: float
    token_amount: float
    price: float  # SOL per token after this trade
    market_cap_sol: float
    timestamp: float
    pool: str = "pump"

    @property
    def is_buy(self) -> bool:
        return self.side in ("buy", "create")

    @classmethod
    def from_pumpportal(cls, msg: dict[str, Any], now: float | None = None) -> "TradeEvent | None":
        side = msg.get("txType")
        mint = msg.get("mint")
        trader = msg.get("traderPublicKey")
        if side not in ("buy", "sell", "create") or not mint or not trader:
            return None
        try:
            sol = float(msg.get("solAmount") or 0.0)
            tokens = float(msg.get("tokenAmount") or msg.get("initialBuy") or 0.0)
            # bonding-curve coins report v*InBondingCurve; graduated (PumpSwap) coins *InPool
            v_sol = float(msg.get("vSolInBondingCurve") or msg.get("solInPool") or 0.0)
            v_tok = float(msg.get("vTokensInBondingCurve") or msg.get("tokensInPool") or 0.0)
            mcap = float(msg.get("marketCapSol") or 0.0)
        except (TypeError, ValueError):
            return None
        if v_sol > 0 and v_tok > 0:
            price = v_sol / v_tok
        elif sol > 0 and tokens > 0:
            price = sol / tokens
        else:
            return None
        return cls(
            signature=str(msg.get("signature", "")),
            mint=mint,
            trader=trader,
            side=side,
            sol_amount=sol,
            token_amount=tokens,
            price=price,
            market_cap_sol=mcap,
            timestamp=now if now is not None else time.time(),
            pool=str(msg.get("pool", "pump")),
        )
