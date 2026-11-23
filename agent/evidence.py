"""EvidenceIndex: resolve the text behind an evidence URL from caches (cheap) before going live."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from agent.cache import DiskCache, get_cache
from agent.research import normalize_url
from agent.tools.base import PageContent

SCRAPE_KEYS = ("httpx.scrape",)
MCP_SCRAPE_KEYS = ("composio_mcp.scrape",)
SEARCH_KEYS = ("exa.search.v2", "composio_mcp.exa_search")


class EvidenceIndex:
    """get_text(url) -> (text, via). Order: httpx page, composio_mcp page, longest Exa snippet, live fetch.

    Page text and snippet are combined when both exist so a quote found in either grounds.
    """

    def __init__(self, cache: Optional[DiskCache] = None, scraper: Any = None, live: bool = True) -> None:
        self.cache = cache or get_cache()
        self._scraper = scraper
        self.live = live
        self._snippets: Optional[dict[str, str]] = None
        self._snip_lock: Optional[asyncio.Lock] = None
        self._text: dict[str, asyncio.Future] = {}
        self._render: dict[str, asyncio.Future] = {}
        self._extra_snippets: dict[str, str] = {}

    # ---------------------------------------------------------- snippets
    def _scan_snippets(self) -> dict[str, str]:
        out: dict[str, str] = {}
        d = self.cache.directory
        if not d.exists():
            return out
        for p in d.glob("*.json"):
            try:
                obj = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(obj, dict) or obj.get("tool") not in SEARCH_KEYS:
                continue
            for r in obj.get("value") or []:
                if not isinstance(r, dict) or not r.get("url"):
                    continue
                snip = r.get("snippet") or ""
                k = normalize_url(r["url"])
                if len(snip) > len(out.get(k, "")):
                    out[k] = snip
        return out

    async def _snippet_for(self, url: str) -> str:
        if self._snippets is None:
            if self._snip_lock is None:
                self._snip_lock = asyncio.Lock()
            async with self._snip_lock:
                if self._snippets is None:
                    self._snippets = await asyncio.to_thread(self._scan_snippets)
        k = normalize_url(url)
        snip, extra = self._snippets.get(k, ""), self._extra_snippets.get(k, "")
        return extra if len(extra) > len(snip) else snip

    def add_snippet(self, url: str, snippet: str) -> None:
        """Register a search snippet seen after the cache scan (L6 re-research); re-resolve that URL."""
        k = normalize_url(url)
        if len(snippet or "") > len(self._extra_snippets.get(k, "")):
            self._extra_snippets[k] = snippet
            fut = self._text.get(url)
            if fut is not None and fut.done():
                self._text.pop(url, None)

    def rendered_text(self, url: str) -> str:
        """Text of a browser render already done for url (L4), else "" (never starts a render)."""
        fut = self._render.get(url)
        if fut is None or not fut.done() or fut.cancelled() or fut.exception() is not None:
            return ""
        return getattr(fut.result(), "text", "") or ""

    # -------------------------------------------------------------- text
    def _cached_page(self, tools: tuple[str, ...], url: str) -> tuple[str, str]:
        for tool in tools:
            hit = self.cache.get(tool, {"url": url})
            if isinstance(hit, dict) and (hit.get("text") or "").strip():
                return hit["text"], tool
        return "", ""

    async def _resolve(self, url: str) -> tuple[str, str]:
        parts: list[tuple[str, str]] = []
        page, via = self._cached_page(SCRAPE_KEYS, url)
        if not page:
            page, via = self._cached_page(MCP_SCRAPE_KEYS, url)
        if page:
            parts.append((page, via))
        snip = await self._snippet_for(url)
        if snip.strip():
            parts.append((snip, "exa.snippet"))
        if not parts and self.live:
            try:
                scraper = self._scraper
                if scraper is None:
                    from agent.tools.direct_tools import HttpxScrape

                    scraper = HttpxScrape()
                pc = await scraper.fetch(url)
                if (pc.text or "").strip():
                    parts.append((pc.text, "live"))
            except Exception:  # noqa: BLE001 - unreachable page == no evidence text
                pass
        if not parts:
            return "", "none"
        return "\n".join(t for t, _ in parts), "+".join(v for _, v in parts)

    async def get_text(self, url: str) -> tuple[str, str]:
        fut = self._text.get(url)
        if fut is None:
            fut = self._text[url] = asyncio.ensure_future(self._resolve(url))
        return await asyncio.shield(fut)

    async def render(self, url: str) -> PageContent:
        fut = self._render.get(url)
        if fut is None:
            from agent.tools import browser

            fut = self._render[url] = asyncio.ensure_future(browser.render(url))
        return await asyncio.shield(fut)
