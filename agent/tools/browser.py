"""Playwright browser-render fallback (Phase 3, L4)."""
from __future__ import annotations

from agent.tools.base import PageContent


async def render(url: str) -> PageContent:
    # TODO(Phase 3): Playwright render for JS-heavy / blocked pages.
    raise NotImplementedError("browser render is implemented in Phase 3")
