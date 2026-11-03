"""One live feed shared by several strategy instances (tactics)."""

from __future__ import annotations

import json
import time

from .feed import PumpPortalFeed


class FeedProxy:
    """Per-tactic view of the shared feed; ref-counts token subscriptions."""

    def __init__(self, mux: "FeedMux"):
        self.mux = mux
        self.tokens: set[str] = set()

    async def subscribe_tokens(self, mints):
        new = set(mints) - self.tokens
        self.tokens |= new
        await self.mux.feed.subscribe_tokens(new)

    async def unsubscribe_tokens(self, mints):
        gone = set(mints) & self.tokens
        self.tokens -= gone
        still = set().union(*(p.tokens for p in self.mux.proxies))
        await self.mux.feed.unsubscribe_tokens(gone - still)

    async def set_accounts(self, wallets):
        pass


class NullFeed:
    def __init__(self):
        self.tokens: set[str] = set()
        self.trade_data_blocked = False

    async def subscribe_tokens(self, m):
        pass

    async def unsubscribe_tokens(self, m):
        pass


class FeedMux:
    def __init__(self, url: str | None, record_path: str | None = None):
        self.bots: list = []
        self.proxies: list[FeedProxy] = []
        self.feed = PumpPortalFeed(url, self.handle) if url else NullFeed()
        self.messages = 0
        self._rec = open(record_path, "a", buffering=1) if record_path else None  # line-buffered: safe if killed

    def record_extra(self, rec: dict) -> None:
        """Market data (SOL price, discovery, holder checks) so replays are exact."""
        if self._rec:
            self._rec.write(json.dumps({"t": time.time(), "x": rec}) + "\n")

    def proxy(self) -> FeedProxy:
        p = FeedProxy(self)
        self.proxies.append(p)
        return p

    async def handle(self, msg: dict) -> None:
        self.messages += 1
        if self._rec:
            self._rec.write(json.dumps({"t": time.time(), "m": msg}) + "\n")
        for b in self.bots:
            try:
                await b.handle(msg)
            except Exception:
                b.log.exception("handler error")
