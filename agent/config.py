"""Environment and path configuration. Never print secret values."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
APPS_JSON = DATA_DIR / "apps.json"
REPORTS_DIR = DATA_DIR / "reports"
HUMAN_LOG = DATA_DIR / "human_log.md"

# Composio tool slugs. Source: docs.composio.dev/toolkits/{exa,firecrawl} (checked 2026-11-23).
# UNVERIFIED against a live key -- confirm in `python -m agent smoke` (it prints the slug that worked).
# EXA and FIRECRAWL are API_KEY toolkits (no Composio-managed OAuth, not NO_AUTH); they run via
# Composio "Instant" (connected_account_id="instant_account", billed to the Composio balance)
# or via your own connected account. There is no NO_AUTH web-search toolkit in the catalog.
COMPOSIO_TOOL_SLUGS: dict[str, Optional[str]] = {
    "search": "EXA_SEARCH",
    "search_fallback": "FIRECRAWL_SEARCH",
    "scrape": "FIRECRAWL_SCRAPE",
    "scrape_fallback": "EXA_GET_CONTENTS_ACTION",
}

load_dotenv(ROOT / ".env")


def require(name: str) -> str:
    """Return env var `name` or raise a clear error naming it (never the value)."""
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name} (set it in .env)")
    return val


@dataclass(frozen=True)
class Settings:
    composio_api_key: Optional[str] = field(default=None, repr=False)
    openai_api_key: Optional[str] = field(default=None, repr=False)
    gemini_api_key: Optional[str] = field(default=None, repr=False)
    exa_api_key: Optional[str] = field(default=None, repr=False)
    composio_user_id: str = "app-intel-agent"
    composio_account: Optional[str] = "instant_account"
    tool_backend: Literal["composio", "direct"] = "composio"
    extractor_model: str = "gpt-5"
    verifier_model: str = "gemini-4-neon"
    concurrency: int = 8

    ROOT: Path = ROOT
    DATA_DIR: Path = DATA_DIR
    CACHE_DIR: Path = CACHE_DIR
    APPS_JSON: Path = APPS_JSON
    REPORTS_DIR: Path = REPORTS_DIR
    HUMAN_LOG: Path = HUMAN_LOG

    def __post_init__(self) -> None:
        if self.tool_backend not in ("composio", "direct"):
            raise ValueError(f"TOOL_BACKEND must be 'composio' or 'direct', got {self.tool_backend!r}")
        if self.concurrency < 1:
            raise ValueError("CONCURRENCY must be >= 1")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    e = os.environ.get
    return Settings(
        composio_api_key=e("COMPOSIO_API_KEY") or None,
        openai_api_key=e("OPENAI_API_KEY") or None,
        gemini_api_key=e("GEMINI_API_KEY") or None,
        exa_api_key=e("EXA_API_KEY") or None,
        composio_user_id=e("COMPOSIO_USER_ID") or "app-intel-agent",
        composio_account=e("COMPOSIO_ACCOUNT") or None if "COMPOSIO_ACCOUNT" in os.environ else "instant_account",
        tool_backend=e("TOOL_BACKEND") or "composio",  # type: ignore[arg-type]
        extractor_model=e("EXTRACTOR_MODEL") or "gpt-5",
        verifier_model=e("VERIFIER_MODEL") or "gemini-4-neon",
        concurrency=int(e("CONCURRENCY") or 8),
    )
