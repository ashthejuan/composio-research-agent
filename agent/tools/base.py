"""Tool interfaces (PRD §3) and backend factory. All calls go through agent.cache."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Optional, Protocol, runtime_checkable

from agent.config import get_settings

CatalogStatus = Literal["available", "unavailable", "unknown"]


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    published: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageContent:
    url: str
    title: str
    text: str
    status: int
    fetched_at: str
    via: str

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class SearchTool(Protocol):
    async def search(
        self, query: str, n: int = 8, include_domains: list[str] | None = None
    ) -> list[SearchResult]: ...


@runtime_checkable
class ScrapeTool(Protocol):
    async def fetch(self, url: str) -> PageContent: ...


@runtime_checkable
class CatalogTool(Protocol):
    async def has_toolkit(self, app_name: str) -> tuple[CatalogStatus, Optional[str]]: ...


class NullCatalog:
    """Used when no COMPOSIO_API_KEY: coverage is simply unknown."""

    async def has_toolkit(self, app_name: str) -> tuple[CatalogStatus, Optional[str]]:
        return "unknown", None


def get_tools(backend: Optional[str] = None) -> tuple[SearchTool, ScrapeTool, CatalogTool]:
    """Return (search, scrape, catalog). Catalog is Composio-backed whenever a key exists (AC-6)."""
    s = get_settings()
    backend = backend or s.tool_backend
    if backend not in ("composio", "direct"):
        raise ValueError(f"unknown tool backend {backend!r}")
    from agent.tools import composio_tools as ct

    catalog: CatalogTool = ct.ComposioCatalog() if s.composio_api_key else NullCatalog()
    if backend == "composio":
        return ct.ComposioSearch(), ct.ComposioScrape(), catalog
    from agent.tools import direct_tools as dt

    return dt.ExaSearch(), dt.HttpxScrape(), catalog
