"""Back-tests every followed trader's entries against *our* exit rules.

When a tracked trader buys, we record their entry price and keep watching the
token. If price reaches +TP% before -SL% (within the horizon) the entry is a
"hit", otherwise a "miss". This gives a per-trader hit rate for exactly the
strategy the bot runs, which is the main input of the entry analyzer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PendingEntry:
    wallet: str
    mint: str
    entry_price: float
    opened_at: float


class EntryOutcomeTracker:
    def __init__(self, tp_pct: float, sl_pct: float, horizon_sec: float, max_pending: int = 2000):
        self.tp = 1 + tp_pct / 100
        self.sl = 1 - sl_pct / 100
        self.horizon = horizon_sec
        self.max_pending = max_pending
        self.pending: dict[str, list[PendingEntry]] = {}

    def __len__(self) -> int:
        return sum(len(v) for v in self.pending.values())

    def mints(self) -> set[str]:
        return set(self.pending)

    def add(self, wallet: str, mint: str, price: float, ts: float) -> None:
        if price <= 0 or len(self) >= self.max_pending:
            return
        entries = self.pending.setdefault(mint, [])
        if any(e.wallet == wallet for e in entries):
            return  # only score a wallet's first entry per token
        entries.append(PendingEntry(wallet, mint, price, ts))

    def on_price(self, mint: str, price: float, ts: float) -> list[tuple[str, bool]]:
        entries = self.pending.get(mint)
        if not entries:
            return []
        resolved, keep = [], []
        for e in entries:
            r = price / e.entry_price
            if r >= self.tp - 1e-12:
                resolved.append((e.wallet, True))
            elif r <= self.sl + 1e-12 or ts - e.opened_at > self.horizon:
                resolved.append((e.wallet, False))
            else:
                keep.append(e)
        if keep:
            self.pending[mint] = keep
        else:
            del self.pending[mint]
        return resolved

    def expire(self, now: float) -> list[tuple[str, bool]]:
        out = []
        for mint in list(self.pending):
            keep = []
            for e in self.pending[mint]:
                if now - e.opened_at > self.horizon:
                    out.append((e.wallet, False))
                else:
                    keep.append(e)
            if keep:
                self.pending[mint] = keep
            else:
                del self.pending[mint]
        return out
