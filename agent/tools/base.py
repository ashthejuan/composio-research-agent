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


def resolve_backend(backend: Optional[str] = None) -> str:
    """Concrete backend: "composio_mcp" (consumer key ck_), "composio_sdk" (project key ak_ / other), "direct".

    "composio" auto-detects by key prefix: a Composio consumer key only authenticates the Composio
    Connect MCP server (the SDK/REST answers 401), a project key only works with the SDK.
    """
    s = get_settings()
    backend = backend or s.tool_backend
    if backend not in ("composio", "composio_sdk", "direct"):
        raise ValueError(f"unknown tool backend {backend!r}")
    if backend == "composio":
        return "composio_mcp" if (s.composio_api_key or "").startswith("ck_") else "composio_sdk"
    return backend


def get_tools(backend: Optional[str] = None) -> tuple[SearchTool, ScrapeTool, CatalogTool]:
    """Return (search, scrape, catalog).

    Catalog: SDK toolkit listing (falls back to the public docs catalog) for project keys; the public
    docs catalog ("composio_docs_catalog") for consumer keys or no key.
    With env TOOL_FALLBACK=direct, Composio search/scrape are wrapped so that a missing connection or
    repeated errors switch to the direct tools (logged once, counted per backend).
    """
    s = get_settings()
    resolved = resolve_backend(backend)
    from agent.tools import composio_tools as ct

    key = s.composio_api_key or ""
    catalog: CatalogTool = (
        ct.FallbackCatalog(ct.ComposioCatalog(), ct.DocsCatalog()) if key and not key.startswith("ck_")
        else ct.DocsCatalog()
    )
    from agent.tools import direct_tools as dt

    if resolved == "direct":
        return dt.ExaSearch(), dt.HttpxScrape(), catalog
    if resolved == "composio_mcp":
        from agent.tools import composio_mcp as mcp

        search, scrape = mcp.MCPSearch(), mcp.MCPScrape()
    else:
        search, scrape = ct.ComposioSearch(), ct.ComposioScrape()
    from agent.tools import fallback as fb

    if fb.fallback_mode() == "direct":
        return (*fb.wrap(search, scrape, dt.ExaSearch(), dt.HttpxScrape(), primary_label=resolved), catalog)
    return search, scrape, catalog
