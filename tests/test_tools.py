import asyncio

import pytest

from agent import cache as cache_mod
from agent.config import Settings, get_settings
from agent.tools import base, smoke
from agent.tools.composio_tools import slug_candidates
from agent.tools.direct_tools import html_to_text


def _settings(**kw):
    d = dict(composio_api_key=None, openai_api_key=None, gemini_api_key=None, exa_api_key=None)
    d.update(kw)
    return Settings(**d)


@pytest.fixture
def patch_settings(monkeypatch, tmp_path):
    def apply(**kw):
        s = _settings(CACHE_DIR=tmp_path, **kw)
        monkeypatch.setattr(base, "get_settings", lambda: s)
        monkeypatch.setattr(smoke, "get_settings", lambda: s)
        monkeypatch.setattr(cache_mod, "get_settings", lambda: s)
        cache_mod.reset_cache()
        return s

    yield apply
    cache_mod.reset_cache()


def test_cache_hit_miss_and_refresh(tmp_path):
    c = cache_mod.DiskCache(tmp_path)
    assert c.get("t", {"a": 1}) is None
    c.set("t", {"a": 1}, {"v": [1, 2]})
    assert c.get("t", {"b": 1}) is None
    assert c.get("t", {"a": 1}) == {"v": [1, 2]}
    assert c.stats() == {"hits": 1, "misses": 2}
    r = cache_mod.DiskCache(tmp_path, refresh=True)
    assert r.get("t", {"a": 1}) is None
    assert not list(tmp_path.glob("*.tmp"))


def test_cached_decorator(tmp_path):
    c = cache_mod.DiskCache(tmp_path)
    calls = []

    @c.cached("f")
    async def f(x):
        calls.append(x)
        return {"x": x}

    assert asyncio.run(f(1)) == asyncio.run(f(1)) == {"x": 1}
    assert calls == [1] and c.hits == 1


def test_html_to_text_strips():
    t, text = html_to_text(
        "<html><head><title>T</title><style>p{}</style></head><body><nav>menu</nav>"
        "<script>var x=1</script><p>Hello <b>world</b></p></body></html>")
    assert t == "T" and text == "Hello world"


def test_slug_candidates():
    assert slug_candidates("Zoho CRM")[0] == "zoho"
    assert slug_candidates("Monday.com")[0] == "monday"
    assert slug_candidates("Google Ads")[:2] == ["googleads", "google_ads"]
    assert "linear" in slug_candidates("Linear")


def test_get_tools_backend(patch_settings):
    from agent.tools import composio_tools as ct, direct_tools as dt

    patch_settings(tool_backend="direct")
    s, sc, cat = base.get_tools()
    assert isinstance(s, dt.ExaSearch) and isinstance(sc, dt.HttpxScrape) and isinstance(cat, base.NullCatalog)
    s, sc, cat = base.get_tools("composio")
    assert isinstance(s, ct.ComposioSearch)
    patch_settings(composio_api_key="k", tool_backend="direct")
    assert isinstance(base.get_tools()[2], ct.ComposioCatalog)
    assert asyncio.run(base.NullCatalog().has_toolkit("x")) == ("unknown", None)


def test_smoke_no_keys(patch_settings, monkeypatch):
    patch_settings(tool_backend="composio")

    async def no_net(self, url):
        raise OSError("no network in tests")

    from agent.tools.direct_tools import HttpxScrape

    monkeypatch.setattr(HttpxScrape, "_get", no_net)
    assert smoke.run_smoke() == 1


# --- review fixes -----------------------------------------------------------------------------

def test_html_to_text_svg_title_and_cells():
    t, text = html_to_text(
        "<html><head><title>Docs</title></head><body><svg><title>GitHub</title></svg>"
        "<table><tr><td>Plan</td><td>Free</td></tr></table><ul><li>a</li><li>b</li></ul></body></html>")
    assert t == "Docs"
    assert "Plan Free" in text and "GitHub" not in text and text.splitlines()[-2:] == ["a", "b"]


def test_cache_rejects_non_json_value(tmp_path):
    c = cache_mod.DiskCache(tmp_path)
    with pytest.raises(TypeError):
        c.set("t", {}, object())
    with pytest.raises(ValueError):
        c.set("t", {}, float("nan"))
    assert not list(tmp_path.iterdir())


def test_composio_search_falls_back_on_empty_and_skips_caching_empty(patch_settings, monkeypatch):
    from agent.tools import composio_tools as ct

    patch_settings(composio_api_key="k")
    calls = []

    async def fake_exec(slug, args):
        calls.append(slug)
        if slug == ct.COMPOSIO_TOOL_SLUGS["search"]:
            return {"results": []}
        return {"data": {"web": [{"url": "https://x.dev", "title": "X", "description": "d"}]}}

    monkeypatch.setattr(ct, "execute_tool", fake_exec)
    r = asyncio.run(ct.ComposioSearch().search("q", n=3))
    assert [x.url for x in r] == ["https://x.dev"] and calls == ["EXA_SEARCH", "FIRECRAWL_SEARCH"]

    async def nothing(slug, args):
        return {}

    monkeypatch.setattr(ct, "execute_tool", nothing)
    assert asyncio.run(ct.ComposioSearch().search("empty", n=3)) == []
    assert cache_mod.get_cache().get("composio.search", {"query": "empty", "n": 3, "domains": []}) is None


def test_composio_scrape_empty_primary_uses_fallback_and_failures_not_cached(patch_settings, monkeypatch):
    from agent.tools import composio_tools as ct

    patch_settings(composio_api_key="k")

    async def fake_exec(slug, args):
        if slug == "FIRECRAWL_SCRAPE":
            return {"data": {"markdown": ""}}
        return {"results": [{"url": args["urls"][0], "title": "T", "text": "body"}]}

    monkeypatch.setattr(ct, "execute_tool", fake_exec)
    page = asyncio.run(ct.ComposioScrape().fetch("https://a.dev"))
    assert page.text == "body" and page.via == "composio:EXA_GET_CONTENTS_ACTION"

    async def empty(slug, args):
        return {}

    monkeypatch.setattr(ct, "execute_tool", empty)
    assert asyncio.run(ct.ComposioScrape().fetch("https://b.dev")).text == ""
    assert cache_mod.get_cache().get("composio.scrape", {"url": "https://b.dev"}) is None


def test_catalog_single_fetch_under_concurrency_and_failure_memo(patch_settings, monkeypatch):
    from agent.tools import composio_tools as ct

    patch_settings(composio_api_key="k")
    n = {"calls": 0}

    def ok_list():
        n["calls"] += 1
        return ["linear", "googleads"]

    cat = ct.ComposioCatalog()
    monkeypatch.setattr(cat, "_list_all_sync", ok_list)

    async def many():
        return await asyncio.gather(*(cat.has_toolkit(a) for a in ["Linear", "Google Ads", "Nope"] * 4))

    res = asyncio.run(many())
    assert n["calls"] == 1 and res[0] == ("available", "linear") and res[2] == ("unavailable", None)

    for f in cache_mod.get_cache().directory.glob("*.json"):  # start cold: no cached catalog
        f.unlink()
    bad = ct.ComposioCatalog()

    async def boom():
        n["calls"] += 1
        raise ConnectionError("down")

    monkeypatch.setattr(bad, "_fetch", boom)
    n["calls"] = 0
    assert asyncio.run(bad.has_toolkit("Linear")) == ("unknown", None)
    assert asyncio.run(bad.has_toolkit("Linear")) == ("unknown", None)
    assert n["calls"] == 1 and isinstance(bad.last_error, ConnectionError)


def test_httpx_scrape_does_not_cache_429_but_caches_404(patch_settings, monkeypatch):
    import httpx

    from agent.tools.direct_tools import HttpxScrape

    patch_settings()
    codes = {"https://rl.dev": 429, "https://gone.dev": 404, "https://pdf.dev": 200}

    async def fake_get(self, url):
        ct = "application/pdf" if "pdf" in url else "text/html"
        return httpx.Response(codes[url], headers={"content-type": ct}, content=b"%PDF-1.7 binary")

    monkeypatch.setattr(HttpxScrape, "_get", fake_get)
    for u in codes:
        asyncio.run(HttpxScrape().fetch(u))
    c = cache_mod.get_cache()
    assert c.get("httpx.scrape", {"url": "https://rl.dev"}) is None
    assert c.get("httpx.scrape", {"url": "https://gone.dev"})["status"] == 404
    assert c.get("httpx.scrape", {"url": "https://pdf.dev"})["text"] == ""


def test_scrub_redacts_configured_secret_values(monkeypatch):
    monkeypatch.setenv("COMPOSIO_API_KEY", "ak_shortKey123")
    out = smoke.scrub("auth failed for ak_shortKey123 and sk-abcdefghijklmnop")
    assert "ak_shortKey123" not in out and "sk-abcdefghijklmnop" not in out


def test_llm_missing_key_fails_fast_without_retry(monkeypatch):
    import time

    from agent import llm

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(llm.extract_json("s", "u", smoke._Ping))
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        asyncio.run(llm.verify_claim("p", smoke._Ping))
    assert time.monotonic() - t0 < 0.5
