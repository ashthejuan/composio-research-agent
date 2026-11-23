"""Playwright browser-render fallback (Phase 3, L4).

One shared headless Chromium per event loop (lazy, lock-guarded); renders are capped by a global
semaphore (env BROWSER_MAX_INFLIGHT, default 3). Only successful, non-empty renders are cached.
"""
from __future__ import annotations

import asyncio
import os
import weakref
from datetime import datetime, timezone
from typing import Any, Optional

from agent.cache import get_cache
from agent.tools.base import PageContent

CACHE_TOOL = "browser.render"
RENDER_TIMEOUT_MS = 25_000
FALLBACK_TIMEOUT_MS = 10_000
BROWSER_MAX_INFLIGHT = int(os.environ.get("BROWSER_MAX_INFLIGHT", "") or 3)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class _State:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(BROWSER_MAX_INFLIGHT)
        self.browser: Any = None
        self.pw: Any = None


# asyncio primitives and the browser connection are loop-bound: one state per event loop.
_states: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _State]" = weakref.WeakKeyDictionary()


def _state() -> _State:
    loop = asyncio.get_running_loop()
    st = _states.get(loop)
    if st is None:
        st = _states[loop] = _State()
    return st


async def _launch() -> tuple[Any, Any]:
    """Start Playwright + headless chromium. Returns (playwright, browser). Patched in tests."""
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except BaseException:
        await pw.stop()
        raise
    return pw, browser


async def _get_browser(st: _State) -> Any:
    async with st.lock:
        if st.browser is not None and not getattr(st.browser, "is_connected", lambda: True)():
            st.browser = None
        if st.browser is None:
            st.pw, st.browser = await _launch()
        return st.browser


async def close_browser() -> None:
    """Close the shared browser of the current event loop (idempotent)."""
    st = _state()
    async with st.lock:
        browser, pw, st.browser, st.pw = st.browser, st.pw, None, None
    for closer in (getattr(browser, "close", None), getattr(pw, "stop", None)):
        if closer is not None:
            try:
                await closer()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass


def _empty(url: str, via: str = "browser") -> PageContent:
    return PageContent(url=url, title="", text="", status=0,
                       fetched_at=datetime.now(timezone.utc).isoformat(), via=via)


async def _goto(page: Any, url: str) -> Any:
    try:
        return await page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - networkidle never settles on chatty pages: settle for DOM ready
        return await page.goto(url, wait_until="domcontentloaded", timeout=FALLBACK_TIMEOUT_MS)


async def render(url: str) -> PageContent:
    cache = get_cache()
    hit = cache.get(CACHE_TOOL, {"url": url})
    if hit is not None:
        return PageContent(**hit)
    st = _state()
    async with st.sem:
        ctx = page = None
        try:
            browser = await _get_browser(st)
            ctx = await browser.new_context(user_agent=UA)
            page = await ctx.new_page()
            resp = await _goto(page, url)
            status = int(getattr(resp, "status", 0) or 0)
            text = await page.evaluate("document.body ? document.body.innerText : ''")
            title = await page.title()
        except Exception:  # noqa: BLE001 - render failures are data (empty page), never raised
            return _empty(url, "browser:error")
        finally:
            for obj in (page, ctx):
                if obj is not None:
                    try:
                        await obj.close()
                    except Exception:  # noqa: BLE001
                        pass
    text = "\n".join(l for l in (" ".join(x.split()) for x in (text or "").splitlines()) if l)
    page_c = PageContent(url=url, title=title or "", text=text, status=status,
                         fetched_at=datetime.now(timezone.utc).isoformat(), via="browser")
    if text and 0 < status < 400:
        cache.set(CACHE_TOOL, {"url": url}, page_c.to_dict())
    return page_c
