import asyncio
import json

from agent.cache import DiskCache
from agent.evidence import EvidenceIndex
from agent.tools import browser
from agent.tools.base import PageContent


def page(url, text, status=200):
    return PageContent(url, "", text, status, "now", "t").to_dict()


class FakeScraper:
    def __init__(self, text=""):
        self.text, self.calls = text, 0

    async def fetch(self, url):
        self.calls += 1
        return PageContent(url, "", self.text, 200 if self.text else 0, "now", "fake")


def exa(cache, url, snippet, tool="exa.search.v2", q="q"):
    cache.set(tool, {"query": q}, [{"url": url, "title": "", "snippet": snippet, "published": None}])


def idx(tmp_path, scraper=None, live=True):
    return EvidenceIndex(cache=DiskCache(tmp_path, refresh=False), scraper=scraper, live=live), DiskCache(tmp_path, refresh=False)


def test_httpx_page_first_and_combined_with_snippet(tmp_path):
    i, c = idx(tmp_path)
    c.set("httpx.scrape", {"url": "https://a.com/x"}, page("https://a.com/x", "PAGE TEXT"))
    exa(c, "https://www.a.com/x/", "snippet words")
    text, via = asyncio.run(i.get_text("https://a.com/x"))
    assert "PAGE TEXT" in text and "snippet words" in text
    assert via == "httpx.scrape+exa.snippet"


def test_mcp_scrape_when_no_httpx(tmp_path):
    i, c = idx(tmp_path)
    c.set("composio_mcp.scrape", {"url": "https://a.com/x"}, page("https://a.com/x", "MCP TEXT"))
    assert asyncio.run(i.get_text("https://a.com/x")) == ("MCP TEXT", "composio_mcp.scrape")


def test_empty_cached_page_skipped_and_longest_snippet_used(tmp_path):
    s = FakeScraper("LIVE")
    i, c = idx(tmp_path, s)
    c.set("httpx.scrape", {"url": "https://a.com/x"}, page("https://a.com/x", "", 403))
    exa(c, "https://a.com/x", "short", q="1")
    exa(c, "https://a.com/x", "a much longer snippet", tool="composio_mcp.exa_search", q="2")
    text, via = asyncio.run(i.get_text("https://a.com/x"))
    assert text == "a much longer snippet" and via == "exa.snippet"
    assert s.calls == 0


def test_live_fetch_then_none_and_memoized(tmp_path):
    s = FakeScraper("LIVE TEXT")
    i, _ = idx(tmp_path, s)

    async def twice():
        return await i.get_text("https://a.com/y"), await i.get_text("https://a.com/y")

    (a, b) = asyncio.run(twice())
    assert a == b == ("LIVE TEXT", "live") and s.calls == 1
    i2, _ = idx(tmp_path, FakeScraper(""))
    assert asyncio.run(i2.get_text("https://a.com/z")) == ("", "none")
    i3, _ = idx(tmp_path, FakeScraper("x"), live=False)
    assert asyncio.run(i3.get_text("https://a.com/z")) == ("", "none")


def test_render_delegates_to_browser(tmp_path, monkeypatch):
    calls = []

    async def fake(url):
        calls.append(url)
        return PageContent(url, "", "rendered", 200, "now", "browser")

    monkeypatch.setattr(browser, "render", fake)
    i, _ = idx(tmp_path)

    async def go():
        return await i.render("https://a.com"), await i.render("https://a.com")

    a, b = asyncio.run(go())
    assert a.text == "rendered" and calls == ["https://a.com"]


# ---------------------------------------------------------------- browser
class FakePage:
    def __init__(self, text, status=200, fail=False):
        self.text, self.status, self.fail, self.closed = text, status, fail, False

    async def goto(self, url, wait_until, timeout):
        if self.fail:
            raise RuntimeError("boom")

        class R:
            pass

        r = R()
        r.status = self.status
        return r

    async def evaluate(self, js):
        return self.text

    async def title(self):
        return "T"

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, **kw):
        self.kw, self.n = kw, 0

    async def new_context(self, **k):
        self.n += 1
        outer = self

        class Ctx:
            async def new_page(self):
                return FakePage(**outer.kw)

            async def close(self):
                pass

        return Ctx()

    async def close(self):
        pass


def patch_launch(monkeypatch, tmp_path, launches, **kw):
    from agent import cache as cmod
    monkeypatch.setattr(cmod, "_default", DiskCache(tmp_path, refresh=False))

    async def launch():
        launches.append(1)
        return None, FakeBrowser(**kw)

    monkeypatch.setattr(browser, "_launch", launch)


def test_browser_render_caches_success_and_shares_browser(tmp_path, monkeypatch):
    launches = []
    patch_launch(monkeypatch, tmp_path, launches, text="Hello\n\n  world  ")

    async def go():
        a = await browser.render("https://a.com/1")
        b = await browser.render("https://a.com/2")
        await browser.close_browser()
        return a, b

    a, b = asyncio.run(go())
    assert a.text == "Hello\nworld" and a.status == 200 and a.via == "browser"
    assert len(launches) == 1
    assert DiskCache(tmp_path, refresh=False).get("browser.render", {"url": "https://a.com/1"})["text"] == "Hello\nworld"


def test_browser_render_never_caches_failures(tmp_path, monkeypatch):
    for kw in ({"text": "", "status": 200}, {"text": "x", "status": 500}, {"text": "x", "fail": True}):
        launches = []
        patch_launch(monkeypatch, tmp_path, launches, **kw)

        async def go():
            p = await browser.render("https://a.com/f")
            await browser.close_browser()
            return p

        p = asyncio.run(go())
        assert not list(tmp_path.glob("*.json")), kw
        if kw.get("fail"):
            assert p.text == "" and p.status == 0
