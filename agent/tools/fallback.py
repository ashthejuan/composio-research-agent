"""Opt-in Composio -> direct fallback (PRD §3 fallback rule). Active ONLY with env TOOL_FALLBACK=direct.

On `ComposioConnectionMissing` / `ComposioUpstreamAuthError` the wrapper switches to the direct tool for the rest of the process.
On other errors it uses the direct tool for that call, and after `max_errors` consecutive primary
failures it stops trying the primary. A WARNING is logged once per wrapper. Calls are counted per
backend so the run manifest can report how many went through Composio vs direct.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Optional

from agent.tools.base import PageContent, ScrapeTool, SearchResult, SearchTool
from agent.tools.composio_mcp import ComposioConnectionMissing, ComposioUpstreamAuthError, scrub

log = logging.getLogger(__name__)


def fallback_mode() -> Optional[str]:
    """'direct' when TOOL_FALLBACK=direct, else None (no fallback)."""
    v = (os.environ.get("TOOL_FALLBACK") or "").strip().lower()
    return v if v == "direct" else None


class BackendCounts:
    """Shared call counter for one (search, scrape) pair: {"composio_mcp": n, "direct": m, "fallbacks": k}."""

    def __init__(self) -> None:
        self.c: Counter[str] = Counter()

    def add(self, key: str, n: int = 1) -> None:
        self.c[key] += n

    def snapshot(self) -> dict[str, int]:
        return dict(self.c)


class _Fallback:
    kind = "call"

    def __init__(self, primary, fallback, *, primary_label: str = "composio_mcp",
                 fallback_label: str = "direct", counts: Optional[BackendCounts] = None,
                 max_errors: int = 3) -> None:
        self.primary, self.fallback = primary, fallback
        self.labels = (primary_label, fallback_label)
        self.counts = counts or BackendCounts()
        self.max_errors = max_errors
        self._errors = 0
        self._disabled = False
        self._warned = False

    def _warn(self, why: str) -> None:
        if not self._warned:
            self._warned = True
            log.warning("composio %s unavailable (%s); TOOL_FALLBACK=direct -> using direct tools",
                        self.kind, scrub(why)[:200])

    async def _run(self, call_primary, call_fallback):
        if not self._disabled:
            try:
                out = await call_primary()
                self._errors = 0
                self.counts.add(self.labels[0])
                return out
            except (ComposioConnectionMissing, ComposioUpstreamAuthError) as e:  # not transient
                self._disabled = True
                self._warn(str(e))
            except Exception as e:  # noqa: BLE001
                self._errors += 1
                if self._errors >= self.max_errors:
                    self._disabled = True
                self._warn(f"{type(e).__name__}: {e}")
            self.counts.add("fallbacks")
        out = await call_fallback()
        self.counts.add(self.labels[1])
        return out


class FallbackSearch(_Fallback):
    kind = "search"

    async def search(self, query: str, n: int = 8, include_domains: list[str] | None = None) -> list[SearchResult]:
        return await self._run(lambda: self.primary.search(query, n=n, include_domains=include_domains),
                               lambda: self.fallback.search(query, n=n, include_domains=include_domains))


class FallbackScrape(_Fallback):
    kind = "scrape"

    async def fetch(self, url: str) -> PageContent:
        return await self._run(lambda: self.primary.fetch(url), lambda: self.fallback.fetch(url))


def wrap(search: SearchTool, scrape: ScrapeTool, d_search: SearchTool, d_scrape: ScrapeTool,
         primary_label: str = "composio_mcp") -> tuple[FallbackSearch, FallbackScrape]:
    counts = BackendCounts()
    return (FallbackSearch(search, d_search, primary_label=primary_label, counts=counts),
            FallbackScrape(scrape, d_scrape, primary_label=primary_label, counts=counts))


def backend_counts(search, scrape) -> Optional[dict[str, int]]:
    """Counter snapshot if the tools are fallback wrappers, else None."""
    c = getattr(search, "counts", None) or getattr(scrape, "counts", None)
    return c.snapshot() if isinstance(c, BackendCounts) else None
