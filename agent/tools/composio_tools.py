"""Composio-SDK-backed tools (composio 0.34.0).

API facts (verified in site-packages source):
- ``Composio(api_key=...).tools.execute(slug, arguments, *, connected_account_id, user_id,
  version, dangerously_skip_version_check, ...)`` -- composio/core/models/tools.py:866.
  Manual execution with version "latest" raises ToolVersionRequiredError (tools.py:842), so we pass
  ``dangerously_skip_version_check=True``.
- ``connected_account_id="instant_account"`` selects Composio-managed Instant accounts (tools.py:889).
- Catalog: ``composio.client.toolkits.list(limit, cursor)`` -> items[].slug, next_cursor
  (composio_client/types/toolkit_list_response.py).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from agent.cache import get_cache
from agent.config import COMPOSIO_TOOL_SLUGS, get_settings, require
from agent.tools.base import CatalogStatus, CatalogTool, PageContent, SearchResult

# Manual app-name -> toolkit slug aliases (checked first).
ALIASES: dict[str, list[str]] = {
    "zoho crm": ["zoho"],
    "google ads": ["googleads"],
    "meta ads": ["metaads", "facebook"],
    "mongodb atlas": ["mongodb"],
    "monday.com": ["monday"],
    # Docs list three highlevel_* toolkits (no bare "highlevel"; its page is legacy/deprecated tools).
    # highlevel_location is the general CRM/marketing/conversations/calendar API; agency is admin-only.
    "gohighlevel": ["highlevel_location", "highlevel_agency", "highlevel_mcp"],
    "netlify": ["netlify_mcp"],
    "pylon": ["pylon_mcp"],
    "otter ai": ["otter_ai_mcp"],
    "devin": ["devin_mcp"],
}

# Apps whose first-word fallback would hit a different product's toolkit (false positive).
# "amazon" is a generic Amazon OAuth toolkit (no Selling Partner API tools), so SP-API is unavailable.
NO_FIRST_WORD: set[str] = {"salesforce commerce cloud", "amazon selling partner"}

log = logging.getLogger(__name__)

_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)


def slug_candidates(app_name: str) -> list[str]:
    """Ordered, de-duplicated candidate toolkit slugs for an app name."""
    name = app_name.strip().lower()
    out: list[str] = list(ALIASES.get(name, []))
    words = re.findall(r"[a-z0-9]+", name)
    if words:
        out += ["".join(words), "_".join(words)]
        if len(words) > 1 and name not in NO_FIRST_WORD:
            out.append(words[0])
    seen: set[str] = set()
    return [c for c in out if not (c in seen or seen.add(c))]


@functools.lru_cache(maxsize=1)
def _client():
    """One shared client: keeps the SDK's per-slug tool-schema cache across calls."""
    from composio import Composio

    return Composio(api_key=require("COMPOSIO_API_KEY"))


def _execute_sync(slug: str, arguments: dict) -> Any:
    s = get_settings()
    kw: dict[str, Any] = dict(user_id=s.composio_user_id, dangerously_skip_version_check=True)
    if s.composio_account:
        kw["connected_account_id"] = s.composio_account
    resp = _client().tools.execute(slug, arguments, **kw)
    if not resp.get("successful", False):
        raise RuntimeError(f"{slug} failed: {str(resp.get('error'))[:300]}")
    return resp.get("data")


@_retry
async def execute_tool(slug: str, arguments: dict) -> Any:
    return await asyncio.to_thread(_execute_sync, slug, arguments)


def _find(obj: Any, pred) -> Any:
    """Depth-first search for the first value satisfying pred."""
    if pred(obj):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = _find(v, pred)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, pred)
            if r is not None:
                return r
    return None


def _first_key(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), str) and obj[k]:
                return obj[k]
        for v in obj.values():
            r = _first_key(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _first_key(v, keys)
            if r:
                return r
    return None


def parse_search(data: Any, n: int) -> list[SearchResult]:
    items = _find(data, lambda o: isinstance(o, list) and o and all(isinstance(x, dict) and "url" in x for x in o)) or []
    out = []
    for it in items[:n]:
        hl = it.get("highlights")
        snippet = it.get("snippet") or it.get("description") or it.get("summary") or (
            " ".join(hl) if isinstance(hl, list) else "") or (it.get("text") or "")[:500]
        out.append(SearchResult(url=it["url"], title=it.get("title") or "", snippet=snippet,
                                published=it.get("publishedDate") or it.get("published")))
    return out


def parse_scrape(data: Any, url: str, via: str) -> PageContent:
    text = _first_key(data, ("markdown", "text", "content")) or ""
    title = _first_key(data, ("title",)) or ""
    code = _find(data, lambda o: isinstance(o, dict) and isinstance(o.get("statusCode"), int))
    status = code["statusCode"] if code else (200 if text else 0)
    return PageContent(url=url, title=title, text=text, status=status,
                       fetched_at=datetime.now(timezone.utc).isoformat(), via=via)


class ComposioSearch:
    async def search(self, query: str, n: int = 8, include_domains: list[str] | None = None) -> list[SearchResult]:
        args = {"query": query, "n": n, "domains": sorted(include_domains or [])}
        cache = get_cache()
        hit = cache.get("composio.search", args)
        if hit is not None:
            return [SearchResult(**r) for r in hit]
        primary = COMPOSIO_TOOL_SLUGS["search"]
        exa_args: dict[str, Any] = {"query": query, "numResults": n}
        if include_domains:
            exa_args["includeDomains"] = include_domains
        fb = COMPOSIO_TOOL_SLUGS.get("search_fallback")
        try:
            results = parse_search(await execute_tool(primary, exa_args), n)
        except Exception:
            if not fb:
                raise
            log.warning("composio search %s failed; trying %s", primary, fb)
            results = []
        if not results and fb:
            results = parse_search(await execute_tool(fb, {"query": query, "limit": n}), n)
        if results:  # an empty list is more likely a parse/shape miss than a real answer: don't pin it
            cache.set("composio.search", args, [r.to_dict() for r in results])
        return results


class ComposioScrape:
    async def fetch(self, url: str) -> PageContent:
        args = {"url": url}
        cache = get_cache()
        hit = cache.get("composio.scrape", args)
        if hit is not None:
            return PageContent(**hit)
        primary = COMPOSIO_TOOL_SLUGS["scrape"]
        fb = COMPOSIO_TOOL_SLUGS.get("scrape_fallback")
        page: Optional[PageContent] = None
        try:
            data = await execute_tool(primary, {"url": url, "formats": ["markdown"]})
            page = parse_scrape(data, url, f"composio:{primary}")
        except Exception:
            if not fb:
                raise
            log.warning("composio scrape %s failed; trying %s", primary, fb)
        if (page is None or not page.text) and fb:
            data = await execute_tool(fb, {"urls": [url], "text": True})
            page = parse_scrape(data, url, f"composio:{fb}")
        assert page is not None
        if page.text:  # empty text = failed/blocked fetch: retry next run instead of caching it
            cache.set("composio.scrape", args, page.to_dict())
        return page


class ComposioCatalog:
    """Toolkit coverage from the Composio catalog (AC-6). Fetches the full list once and caches it."""

    source = "composio_catalog"

    def __init__(self) -> None:
        self._slugs: Optional[set[str]] = None
        self._lock = asyncio.Lock()
        self.last_error: Optional[BaseException] = None

    @staticmethod
    def _list_all_sync() -> list[str]:
        client = _client().client
        slugs: list[str] = []
        cursor = None
        for _ in range(50):  # bounded pagination
            kw: dict[str, Any] = {"limit": 1000}
            if cursor:
                kw["cursor"] = cursor
            page = client.toolkits.list(**kw)
            slugs += [i.slug.lower() for i in page.items]
            cursor = page.next_cursor
            if not cursor:
                break
        return slugs

    @_retry
    async def _fetch(self) -> list[str]:
        return await asyncio.to_thread(self._list_all_sync)

    async def _load(self) -> set[str]:
        async with self._lock:  # concurrent callers share one catalog download
            if self.last_error is not None:  # failed after retries: don't re-pay backoff per app
                raise self.last_error
            if self._slugs is None:
                cache = get_cache()
                hit = cache.get("composio.toolkits.list", {})
                if not hit:
                    try:
                        hit = await self._fetch()
                    except Exception as e:
                        self.last_error = e
                        log.warning("composio catalog unavailable (%s); coverage will be 'unknown'",
                                    type(e).__name__)
                        raise
                    if hit:  # never cache an empty catalog
                        cache.set("composio.toolkits.list", {}, hit)
                self._slugs = set(hit)
            return self._slugs

    async def has_toolkit(self, app_name: str) -> tuple[CatalogStatus, Optional[str]]:
        try:
            slugs = await self._load()
        except Exception:  # logged once in _load; detail kept in self.last_error
            return "unknown", None
        if not slugs:
            return "unknown", None
        for c in slug_candidates(app_name):
            if c in slugs:
                return "available", c
        return "unavailable", None


_DOCS_URL = "https://docs.composio.dev/toolkits"
# Slug must end at a delimiter (optionally after one "/"), so /toolkits/x/tools/..., /toolkits/x.png
# and the bare /toolkits index never match.
_SLUG_RE = re.compile(r"/toolkits/([a-z0-9_\-]+)(?=/?(?:[?#\"'\s<>)\\]|$))", re.I)


def parse_docs_slugs(html: str) -> list[str]:
    """Unique lowercase toolkit slugs found in the docs catalog page, in first-seen order."""
    return list(dict.fromkeys(m.lower() for m in _SLUG_RE.findall(html)))


def _norm(slug: str) -> str:
    return re.sub(r"[_\-]", "", slug.lstrip("_").lower())


class DocsCatalog:
    """Toolkit coverage scraped from Composio's public docs catalog (no API key needed)."""

    source = "composio_docs_catalog"  # read by research.catalog_lookup so the record is labelled right

    def __init__(self, url: str = _DOCS_URL) -> None:
        self._url = url
        self._index: Optional[dict[str, str]] = None  # exact slug and normalised variant -> real slug
        self._lock = asyncio.Lock()
        self.last_error: Optional[BaseException] = None

    @_retry
    async def _fetch(self) -> list[str]:
        import httpx

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(self._url)
            r.raise_for_status()
        return parse_docs_slugs(r.text)

    async def _load(self) -> dict[str, str]:
        async with self._lock:
            if self.last_error is not None:
                raise self.last_error
            if self._index is None:
                cache = get_cache()
                slugs = cache.get("composio.docs.toolkits", {})
                if not slugs:
                    try:
                        slugs = await self._fetch()
                    except Exception as e:
                        self.last_error = e
                        log.warning("composio docs catalog unavailable (%s)", type(e).__name__)
                        raise
                    if not slugs:
                        self.last_error = RuntimeError("docs catalog parsed empty")
                        raise self.last_error
                    cache.set("composio.docs.toolkits", {}, slugs)
                index: dict[str, str] = {}
                for s in slugs:  # variants first so exact slugs always win
                    index.setdefault(_norm(s), s)
                for s in slugs:
                    index[s] = s
                self._index = index
            return self._index

    async def has_toolkit(self, app_name: str) -> tuple[CatalogStatus, Optional[str]]:
        try:
            index = await self._load()
        except Exception:
            return "unknown", None
        for c in slug_candidates(app_name):
            hit = index.get(c) or index.get(_norm(c))
            if hit:
                return "available", hit
        return "unavailable", None


class FallbackCatalog:
    """Tries the primary catalog; if it cannot load ("unknown"), uses the secondary."""

    def __init__(self, primary: CatalogTool, secondary: CatalogTool,
                 primary_source: str = "composio_catalog",
                 secondary_source: str = "composio_docs_catalog") -> None:
        self._primary, self._secondary = primary, secondary
        self._sources = (primary_source, secondary_source)

    async def lookup(self, app_name: str) -> tuple[CatalogStatus, Optional[str], str]:
        status, slug = await self._primary.has_toolkit(app_name)
        if status != "unknown":
            return status, slug, self._sources[0]
        status, slug = await self._secondary.has_toolkit(app_name)
        return status, slug, self._sources[1]

    async def has_toolkit(self, app_name: str) -> tuple[CatalogStatus, Optional[str]]:
        status, slug, _ = await self.lookup(app_name)
        return status, slug
