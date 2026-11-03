"""PumpPortal websocket client with automatic reconnect and resubscribe.

PumpPortal asks clients to use a single connection and send all subscribe
messages over it, which is what this class does.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterable

log = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[None]]


class PumpPortalFeed:
    def __init__(self, url: str, handler: Handler):
        self.url = url
        self.handler = handler
        self.tokens: set[str] = set()
        self.accounts: set[str] = set()
        self._ws = None
        self._stop = asyncio.Event()
        self._notices: set[str] = set()
        self.trade_data_blocked = False  # PumpPortal refused trade streams (key unfunded)

    async def _send(self, method: str, keys: Iterable[str] | None = None) -> None:
        if self._ws is None:
            return  # will be (re)sent on connect
        payload: dict = {"method": method}
        if keys is not None:
            keys = list(keys)
            if not keys:
                return
            payload["keys"] = keys
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as e:  # connection dropped; reconnect resubscribes
            log.debug("send failed: %s", e)

    async def subscribe_tokens(self, mints: Iterable[str]) -> None:
        new = set(mints) - self.tokens
        self.tokens |= new
        await self._send("subscribeTokenTrade", new)

    async def unsubscribe_tokens(self, mints: Iterable[str]) -> None:
        gone = set(mints) & self.tokens
        self.tokens -= gone
        await self._send("unsubscribeTokenTrade", gone)

    async def set_accounts(self, wallets: Iterable[str]) -> None:
        wallets = set(wallets)
        await self._send("unsubscribeAccountTrade", self.accounts - wallets)
        await self._send("subscribeAccountTrade", wallets - self.accounts)
        self.accounts = wallets

    async def run(self) -> None:
        import websockets

        backoff = 1
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                    self._ws = ws
                    backoff = 1
                    log.info("connected to %s", self.url.split("?")[0])
                    await self._send("subscribeNewToken")
                    await self._send("subscribeTokenTrade", self.tokens)
                    await self._send("subscribeAccountTrade", self.accounts)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict) and "message" in msg and "txType" not in msg:
                            note = str(msg["message"])
                            if "only available when connecting with an API key" in note:
                                self.trade_data_blocked = True
                            if note not in self._notices:
                                self._notices.add(note)
                                level = logging.INFO if note.lower().startswith("success") else logging.WARNING
                                log.log(level, "pumpportal: %s", note)
                            continue
                        if isinstance(msg, dict) and "txType" in msg:
                            try:
                                await self.handler(msg)
                            except Exception:
                                log.exception("handler error")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("feed disconnected (%s); retrying in %ss", e, backoff)
            finally:
                self._ws = None
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def stop(self) -> None:
        self._stop.set()
