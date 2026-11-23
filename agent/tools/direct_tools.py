"""Direct (non-Composio) fallback tools: Exa search + httpx scrape."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from agent.cache import get_cache
from agent.config import get_settings, require
from agent.tools.base import PageContent, SearchResult

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TRANSIENT = {408, 425, 429}
_retry = retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "nav", "noscript", "svg", "head", "footer", "template", "iframe"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section", "article", "table",
             "ul", "ol", "dl", "dt", "dd", "blockquote", "header", "main", "aside", "hr"}
    CELL = {"td", "th"}  # keep table cells apart ("<td>a</td><td>b</td>" -> "a b", not "ab")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_title = False
        self._title_done = False  # only the first <title> (later ones are e.g. inline <svg><title>)
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title" and not self._title_done:
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        elif tag in self.CELL:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self._in_title = False
            self._title_done = True
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) with script/style/nav stripped and whitespace collapsed."""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    lines = (" ".join(l.split()) for l in "".join(p.parts).splitlines())
    return p.title.strip(), "\n".join(l for l in lines if l)


class ExaSearch:
    def _search_sync(self, query: str, n: int, include_domains):
        from exa_py import Exa

        # exa-py 2.x: search_and_contents is deprecated; search(contents=...) is the replacement.
        kw: dict = {"num_results": n, "contents": {"text": {"max_characters": 800}}}
        if include_domains:
            kw["include_domains"] = include_domains
        return Exa(api_key=require("EXA_API_KEY")).search(query, **kw)

    async def search(self, query: str, n: int = 8, include_domains: list[str] | None = None) -> list[SearchResult]:
        args = {"query": query, "n": n, "domains": sorted(include_domains or [])}
        cache = get_cache()
        hit = cache.get("exa.search", args)
        if hit is not None:
            return [SearchResult(**r) for r in hit]
        require("EXA_API_KEY")  # fail fast: a missing key is not worth three backoff retries
        resp = await _retry(asyncio.to_thread)(self._search_sync, query, n, include_domains)
        out = [SearchResult(url=r.url, title=r.title or "", snippet=(getattr(r, "text", None) or "")[:500],
                            published=getattr(r, "published_date", None)) for r in resp.results]
        if out:
            cache.set("exa.search", args, [r.to_dict() for r in out])
        return out


class HttpxScrape:
    @_retry
    async def _get(self, url: str) -> httpx.Response:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
            if r.status_code >= 500:
                r.raise_for_status()
            return r

    async def fetch(self, url: str) -> PageContent:
        cache = get_cache()
        hit = cache.get("httpx.scrape", {"url": url})
        if hit is not None:
            return PageContent(**hit)
        now = datetime.now(timezone.utc).isoformat()
        try:
            r = await self._get(url)
        except httpx.HTTPError:
            return PageContent(url, "", "", 0, now, "httpx")  # not cached: transient
        title, text = ("", "")
        ctype = r.headers.get("content-type", "").lower()
        if r.status_code < 400:
            if "html" in ctype or not ctype:
                title, text = html_to_text(r.text)
            elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
                text = r.text
            # else binary (pdf, images): leave text empty; L4 browser / other loops handle it
        page = PageContent(url=url, title=title, text=text, status=r.status_code, fetched_at=now, via="httpx")
        if r.status_code not in _TRANSIENT:  # 408/429 are rate limits/timeouts, not facts about the page
            cache.set("httpx.scrape", {"url": url}, page.to_dict())
        return page
