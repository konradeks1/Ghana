"""Fetch a new coin's metadata and check its social links (website / X / Instagram).

Pump.fun coins store name, description and links in a JSON file on IPFS (the
``uri`` of the create event). Public IPFS gateways rate-limit heavily, so we
try several in turn.

Links are found both in the dedicated fields (``website``, ``twitter``,
``telegram``) and anywhere in the description, since Instagram has no field of
its own. Junk links (pump.fun itself, explorers, chart sites, bare x.com) are
ignored. A link that another coin already used is flagged: scammers often
copy a real project's links onto a fake coin.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

URL_RE = re.compile(r"(?:https?://|www\.)[^\s\"'<>()\[\]]+", re.I)
TWITTER_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}
INSTAGRAM_HOSTS = {"instagram.com", "instagr.am"}
TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.org"}
X_NON_PROFILES = {"search", "home", "explore", "hashtag", "intent", "share", "i", "settings", "messages", "notifications"}
IG_NON_PROFILES = {"p", "reel", "reels", "explore", "stories", "accounts"}
# links that don't prove a real project exists
JUNK_HOSTS = {
    "pump.fun", "pumpportal.fun", "dexscreener.com", "solscan.io", "birdeye.so", "gmgn.ai",
    "photon-sol.tinyastro.io", "bullx.io", "ipfs.io", "raydium.io", "jup.ag", "google.com",
    "youtube.com", "youtu.be", "tiktok.com", "linktr.ee", "github.com", "medium.com",
}


def _host(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _path(url: str) -> str:
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return urlparse(url.split("?")[0].split("#")[0]).path.strip("/")


def classify(url: str) -> tuple[str, str] | None:
    """Return (kind, normalised link) or None for junk."""
    url = url.strip().rstrip(".,;!")
    host = _host(url)
    if not host or "." not in host:
        return None
    path = _path(url)
    parts = [p for p in path.split("/") if p]
    if host in TWITTER_HOSTS:
        # only the coin's own profile or community counts; a link to someone
        # else's tweet, a search or a hashtag says nothing about the project
        if len(parts) >= 3 and parts[0] == "i" and parts[1] == "communities":
            return "twitter", f"x.com/i/communities/{parts[2]}"
        if len(parts) == 1 and parts[0].lower() not in X_NON_PROFILES:
            return "twitter", f"x.com/{parts[0].lower()}"
        return None
    if host in INSTAGRAM_HOSTS:
        if len(parts) == 1 and parts[0].lower() not in IG_NON_PROFILES:
            return "instagram", f"instagram.com/{parts[0].lower()}"
        return None
    if host in TELEGRAM_HOSTS:
        return ("telegram", f"t.me/{path.lower()}") if path else None
    if host in JUNK_HOSTS or any(host.endswith("." + j) for j in JUNK_HOSTS):
        return None
    return "website", host


def extract_socials(meta: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    candidates = []
    for key in ("website", "twitter", "telegram", "instagram"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    for key in ("description", "extensions"):
        v = meta.get(key)
        if isinstance(v, dict):
            candidates += [x for x in v.values() if isinstance(x, str)]
        elif isinstance(v, str):
            candidates += URL_RE.findall(v)
    for c in candidates:
        res = classify(c)
        if res and res[0] not in found:
            found[res[0]] = res[1]
    return found


def check_socials(socials: dict[str, str] | None, required: list[str], mode: str) -> tuple[bool, str]:
    if socials is None:
        return False, "socials not checked yet"
    if not required:
        return True, ""
    have = [k for k in required if k in socials]
    if mode == "all":
        missing = [k for k in required if k not in socials]
        return (not missing, f"missing {'/'.join(missing)}" if missing else "")
    return (bool(have), "" if have else f"no {'/'.join(required)} link")


def gateway_urls(uri: str, gateways: list[str]) -> list[str]:
    if "/ipfs/" not in uri:
        return [uri]
    cid = uri.split("/ipfs/", 1)[1].split("?")[0].strip("/")
    urls = [g.format(cid=cid) for g in gateways]
    if _host(uri) not in ("ipfs.io",) and uri not in urls:
        urls.insert(0, uri)  # a dedicated gateway (e.g. pinata) usually works directly
    return urls


class SocialsChecker:
    def __init__(self, gateways: list[str], http=None, concurrency: int = 8, retries: int = 3, sleep=asyncio.sleep):
        self.gateways = gateways
        self._http = http
        self._sem = asyncio.Semaphore(concurrency)
        self.retries = retries
        self._sleep = sleep
        self.link_owner: dict[str, str] = {}  # normalised link -> first mint that used it

    async def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=6, follow_redirects=True)
        return self._http

    async def fetch_metadata(self, uri: str) -> dict[str, Any] | None:
        if not uri:
            return None
        http = await self._client()
        async with self._sem:
            for attempt in range(self.retries):
                for url in gateway_urls(uri, self.gateways):
                    try:
                        r = await http.get(url)
                        if r.status_code == 200:
                            data = r.json()
                            if isinstance(data, dict):
                                return data
                    except Exception:
                        continue
                await self._sleep(2.0 * (attempt + 1))  # freshly pinned files can lag
        return None

    def reused_links(self, mint: str, socials: dict[str, str]) -> list[str]:
        """Links already used by a *different* coin (copycat / recycled scam)."""
        reused = []
        for kind, link in socials.items():
            owner = self.link_owner.setdefault(link, mint)
            if owner != mint:
                reused.append(f"{kind} {link}")
        return reused

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
