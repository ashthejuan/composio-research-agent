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
# Slugs and input schemas confirmed live via COMPOSIO_GET_TOOL_SCHEMAS on the Connect MCP server (2026-11-23).
# SDK path (ak_ keys) uses all four; the MCP path (ck_ keys, agent/tools/composio_mcp.py) uses "search" and
# scrapes with EXA_GET_CONTENTS_ACTION then FIRECRAWL_SCRAPE (each needs an active connection).
# EXA and FIRECRAWL are API_KEY toolkits (no Composio-managed OAuth, not NO_AUTH); they run via
# Composio "Instant" (connected_account_id="instant_account", billed to the Composio balance)
# or via your own connected account. There is no NO_AUTH web-search toolkit in the catalog.
COMPOSIO_TOOL_SLUGS: dict[str, Optional[str]] = {
    "search": "EXA_SEARCH",
    "search_fallback": "FIRECRAWL_SEARCH",
    "scrape": "FIRECRAWL_SCRAPE",
    "scrape_fallback": "EXA_GET_CONTENTS_ACTION",
}

# "composio": consumer key (ck_) -> Composio Connect MCP; project key (ak_) -> SDK (auto-detected).
# "composio_sdk": force the SDK path. "direct": Exa SDK + httpx.
TOOL_BACKENDS = ("composio", "composio_sdk", "direct")

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
    meta_api_key: Optional[str] = field(default=None, repr=False)
    exa_api_key: Optional[str] = field(default=None, repr=False)
    composio_user_id: str = "app-intel-agent"
    composio_account: Optional[str] = "instant_account"
    composio_mcp_url: str = "https://connect.composio.dev/mcp"
    composio_max_inflight: int = 5
    tool_backend: Literal["composio", "composio_sdk", "direct"] = "composio"
    extractor_model: str = "gpt-6-luna"
    verifier_model: str = "muse-spark-1.3-contributor"
    verifier_base_url: str = "https://api.meta.ai/v1"
    concurrency: int = 8

    ROOT: Path = ROOT
    DATA_DIR: Path = DATA_DIR
    CACHE_DIR: Path = CACHE_DIR
    APPS_JSON: Path = APPS_JSON
    REPORTS_DIR: Path = REPORTS_DIR
    HUMAN_LOG: Path = HUMAN_LOG

    def __post_init__(self) -> None:
        if self.tool_backend not in TOOL_BACKENDS:
            raise ValueError(f"TOOL_BACKEND must be one of {TOOL_BACKENDS}, got {self.tool_backend!r}")
        if self.composio_max_inflight < 1:
            raise ValueError("COMPOSIO_MAX_INFLIGHT must be >= 1")
        if self.concurrency < 1:
            raise ValueError("CONCURRENCY must be >= 1")


KNOWN_VERIFIER_MODELS = (
    "muse-spark-1.3-contributor", "muse-spark-1.3", "muse-spark-1.2-contributor", "muse-spark-1.2",
    "muse-spark-1.1", "muse-ember-1.0", "muse-wonder-1.0",
)


def verifier_model_hint(model: Optional[str] = None) -> Optional[str]:
    """Warning text if the configured verifier model is not a known-good ID, else None (never raises)."""
    m = model or get_settings().verifier_model
    if m in KNOWN_VERIFIER_MODELS:
        return None
    return f"VERIFIER_MODEL={m!r} is not a known Meta model ID; expected one of {', '.join(KNOWN_VERIFIER_MODELS)}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    e = os.environ.get
    return Settings(
        composio_api_key=e("COMPOSIO_API_KEY") or None,
        openai_api_key=e("OPENAI_API_KEY") or None,
        meta_api_key=e("META_API_KEY") or None,
        exa_api_key=e("EXA_API_KEY") or None,
        composio_user_id=e("COMPOSIO_USER_ID") or "app-intel-agent",
        composio_account=e("COMPOSIO_ACCOUNT") or None if "COMPOSIO_ACCOUNT" in os.environ else "instant_account",
        composio_mcp_url=e("COMPOSIO_MCP_URL") or "https://connect.composio.dev/mcp",
        composio_max_inflight=int(e("COMPOSIO_MAX_INFLIGHT") or 5),
        tool_backend=e("TOOL_BACKEND") or "composio",  # type: ignore[arg-type]
        extractor_model=e("EXTRACTOR_MODEL") or "gpt-6-luna",
        verifier_model=e("VERIFIER_MODEL") or "muse-spark-1.3-contributor",
        verifier_base_url=e("VERIFIER_BASE_URL") or "https://api.meta.ai/v1",
        concurrency=int(e("CONCURRENCY") or 8),
    )
