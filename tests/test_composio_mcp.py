"""Composio Connect MCP backend: no network (httpx.MockTransport)."""
import asyncio
import json

import httpx
import pytest

from agent import cache as cache_mod
from agent.config import Settings
from agent.tools import base, composio_mcp as mcp, fallback as fb
from agent.tools.base import PageContent, SearchResult

KEY = "ck_TESTsecretVALUE123456"


def _settings(**kw):
    d = dict(composio_api_key=KEY, openai_api_key=None, meta_api_key=None, exa_api_key=None)
    d.update(kw)
    return Settings(**d)


@pytest.fixture
def patch_settings(monkeypatch, tmp_path):
    def apply(**kw):
        s = _settings(CACHE_DIR=tmp_path, **kw)
        for mod in (base, mcp, cache_mod):
            monkeypatch.setattr(mod, "get_settings", lambda s=s: s)
        cache_mod.reset_cache()
        return s

    monkeypatch.delenv("TOOL_FALLBACK", raising=False)
    apply()
    yield apply
    cache_mod.reset_cache()


def sse(obj) -> str:
    return f"event: message\ndata: {json.dumps(obj)}\n\n"


def tool_reply(rid, inner) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}}


class FakeServer:
    """Minimal MCP server: initialize -> session id; tools/call -> canned inner JSON."""

    def __init__(self, handler, expire_once=False):
        self.handler, self.expire_once = handler, expire_once
        self.requests: list[httpx.Request] = []
        self.sessions = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        body = json.loads(req.content)
        if body["method"] == "initialize":
            self.sessions += 1
            return httpx.Response(200, headers={"mcp-session-id": f"sess-{self.sessions}",
                                                "content-type": "text/event-stream"},
                                  text=sse({"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "x"}}))
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if self.expire_once and req.headers.get("mcp-session-id") == "sess-1":
            self.expire_once = False
            return httpx.Response(404, text="session not found")
        inner = self.handler(body["params"]["name"], body["params"]["arguments"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(tool_reply(body["id"], inner)))


def client_for(server) -> mcp.ComposioMCPClient:
    return mcp.ComposioMCPClient(api_key=KEY, url="https://mcp.test/mcp", transport=httpx.MockTransport(server))


def multi(results, ok=True):
    return {"successful": ok, "data": {"results": results}, "error": None if ok else "1 out of 1 tools failed"}


# ---------------------------------------------------------------- parsing
def test_parse_sse_multiline_and_noise():
    text = ": ping\n\nevent: message\ndata: {\"a\":\ndata: 1}\n\ndata: not json\n\ndata: {\"id\": 2, \"result\": {}}\n"
    assert mcp.parse_sse(text) == [{"a": 1}, {"id": 2, "result": {}}]


def test_parse_rpc_response_json_and_sse_and_id_match():
    assert mcp.parse_rpc_response('{"id": 1, "result": {"x": 1}}', "application/json", 1)["result"] == {"x": 1}
    body = sse({"id": 9, "result": {"x": "other"}}) + sse({"id": 3, "result": {"x": "mine"}})
    assert mcp.parse_rpc_response(body, "text/event-stream", 3)["result"] == {"x": "mine"}
    with pytest.raises(mcp.ComposioMCPError):
        mcp.parse_rpc_response("garbage " + KEY, "application/json", 1)


def test_parse_tool_result_inner_json_and_rpc_error():
    assert mcp.parse_tool_result(tool_reply(1, {"successful": True, "data": {"k": 1}}))["data"] == {"k": 1}
    with pytest.raises(mcp.ComposioMCPError):
        mcp.parse_tool_result({"id": 1, "error": {"code": -32000, "message": "bad"}})


def test_multi_execute_success_and_errors():
    ok = multi([{"response": {"successful": True, "data": {"results": [{"url": "u"}]}}, "tool_slug": "EXA_SEARCH"}])
    assert mcp.parse_multi_execute("EXA_SEARCH", ok) == {"results": [{"url": "u"}]}

    missing = multi([{"error": "No active connection found for toolkit(s) 'exa' in this session.",
                      "tool_slug": "EXA_SEARCH", "index": 0}], ok=False)
    with pytest.raises(mcp.ComposioConnectionMissing) as ei:
        mcp.parse_multi_execute("EXA_SEARCH", missing)
    assert ei.value.toolkit == "exa"

    auth = multi([{"response": {"successful": False, "data": {"message": "Unauthorized", "status_code": 401},
                                "error": "Unauthorized"}, "error": "Unauthorized", "tool_slug": "EXA_SEARCH"}], ok=False)
    with pytest.raises(mcp.ComposioUpstreamAuthError) as ea:
        mcp.parse_multi_execute("EXA_SEARCH", auth)
    assert ea.value.status_code == 401

    other = multi([{"response": {"successful": False, "data": {}, "error": "bad arg"}, "tool_slug": "EXA_SEARCH"}],
                  ok=False)
    with pytest.raises(mcp.ComposioToolError) as eo:
        mcp.parse_multi_execute("EXA_SEARCH", other)
    assert not isinstance(eo.value, mcp.ComposioConnectionMissing) and "bad arg" in str(eo.value)

    with pytest.raises(mcp.ComposioToolError):
        mcp.parse_multi_execute("EXA_SEARCH", {"successful": True, "data": {"results": []}})


def test_scrub_removes_key(patch_settings):
    e = mcp.ComposioToolError("X", f"header x-consumer-api-key: {KEY} and ck_otherKEY999999")
    assert KEY not in str(e) and "ck_otherKEY999999" not in str(e) and "[REDACTED]" in str(e)


# ---------------------------------------------------------------- client
def test_session_init_once_and_header_reuse(patch_settings):
    srv = FakeServer(lambda name, args: {"successful": True, "data": {"name": name}})
    c = client_for(srv)

    async def go():
        return await asyncio.gather(*(c.call_tool("COMPOSIO_SEARCH_TOOLS", {}) for _ in range(5)))

    res = asyncio.run(go())
    assert all(r["data"]["name"] == "COMPOSIO_SEARCH_TOOLS" for r in res)
    assert srv.sessions == 1
    calls = [r for r in srv.requests if json.loads(r.content)["method"] == "tools/call"]
    assert len(calls) == 5 and all(r.headers["mcp-session-id"] == "sess-1" for r in calls)
    assert all(r.headers["x-consumer-api-key"] == KEY for r in srv.requests)
    assert "text/event-stream" in srv.requests[0].headers["accept"]


def test_session_expiry_reinitialises(patch_settings):
    srv = FakeServer(lambda name, args: {"successful": True, "data": 1}, expire_once=True)
    c = client_for(srv)
    assert asyncio.run(c.call_tool("T", {}))["data"] == 1
    assert srv.sessions == 2 and srv.requests[-1].headers["mcp-session-id"] == "sess-2"


def test_http_error_is_scrubbed(patch_settings):
    def h(req):
        return httpx.Response(401, text=f"bad key {KEY}")

    c = mcp.ComposioMCPClient(api_key=KEY, url="https://mcp.test/mcp", transport=httpx.MockTransport(h))
    with pytest.raises(mcp.ComposioMCPError) as ei:
        asyncio.run(c.call_tool("T", {}))
    assert KEY not in str(ei.value)


def test_execute_sends_multi_execute_shape(patch_settings):
    seen = {}

    def h(name, args):
        seen["name"], seen["args"] = name, args
        return multi([{"response": {"successful": True, "data": {"ok": 1}}, "tool_slug": "EXA_SEARCH"}])

    assert asyncio.run(client_for(FakeServer(h)).execute("EXA_SEARCH", {"query": "q"})) == {"ok": 1}
    assert seen["name"] == "COMPOSIO_MULTI_EXECUTE_TOOL"
    assert seen["args"] == {"tools": [{"tool_slug": "EXA_SEARCH", "arguments": {"query": "q"}}],
                            "sync_response_to_workbench": False}


def test_connection_status_and_initiate(patch_settings):
    def h(name, args):
        if args["toolkits"][0].get("action") == "list":
            return {"successful": True, "data": {"results": {
                "exa": {"toolkit": "exa", "status": "active", "accounts": [{"id": "a", "status": "active"}]},
                "firecrawl": {"toolkit": "firecrawl", "status": "initiated", "accounts": []}}}}
        return {"successful": True, "data": {"results": {"exa": {"redirect_url": "https://connect.composio.dev/link/x"}}}}

    c = client_for(FakeServer(h))
    st = asyncio.run(c.connection_status(["exa", "firecrawl"]))
    assert st["exa"]["active"] and not st["firecrawl"]["active"] and st["firecrawl"]["status"] == "initiated"
    assert asyncio.run(c.initiate_connection("exa")) == "https://connect.composio.dev/link/x"


# ---------------------------------------------------------------- tools
class FakeClient:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    async def execute(self, slug, args):
        self.calls.append((slug, args))
        return self.fn(slug, args)


def test_mcp_search_args_snippets_and_cache(patch_settings):
    long = "x" * 9000
    fc = FakeClient(lambda s, a: {"results": [{"url": "https://a.dev", "title": "A", "text": long},
                                              {"url": "https://b.dev", "title": "B", "highlights": ["h"]}]})
    t = mcp.MCPSearch(client=fc)
    r = asyncio.run(t.search("q", n=2, include_domains=["a.dev"]))
    assert [x.url for x in r] == ["https://a.dev", "https://b.dev"]
    assert len(r[0].snippet) == 8000 and r[1].snippet == "h"
    slug, args = fc.calls[0]
    assert slug == "EXA_SEARCH" and args["numResults"] == 2 and args["includeDomains"] == ["a.dev"]
    assert args["contents"] == {"text": {"maxCharacters": 8000}}
    asyncio.run(t.search("q", n=2, include_domains=["a.dev"]))
    assert len(fc.calls) == 1  # cache hit


def test_mcp_search_errors_not_cached_and_missing_raises(patch_settings):
    def boom(s, a):
        raise mcp.ComposioConnectionMissing(s, "exa")

    t = mcp.MCPSearch(client=FakeClient(boom))
    with pytest.raises(mcp.ComposioConnectionMissing):
        asyncio.run(t.search("q", n=2))
    assert cache_mod.get_cache().get(mcp.SEARCH_TOOL, {"query": "q", "n": 2, "domains": []}) is None


def test_mcp_scrape_exa_then_firecrawl_and_missing(patch_settings):
    def fn(slug, args):
        if slug == "EXA_GET_CONTENTS_ACTION":
            return {"results": [{"url": "https://other", "text": "no"}, {"url": args["urls"][0], "title": "T", "text": "body"}]}
        raise AssertionError("firecrawl should not be called")

    p = asyncio.run(mcp.MCPScrape(client=FakeClient(fn)).fetch("https://a.dev"))
    assert p.text == "body" and p.title == "T" and p.via == "composio_mcp:EXA_GET_CONTENTS_ACTION"

    def exa_err(slug, args):
        if slug == "EXA_GET_CONTENTS_ACTION":
            raise mcp.ComposioToolError(slug, "fail")
        return {"data": {"markdown": "# md", "metadata": {"title": "F", "statusCode": 200}}}

    p = asyncio.run(mcp.MCPScrape(client=FakeClient(exa_err)).fetch("https://b.dev"))
    assert p.text == "# md" and p.via == "composio_mcp:FIRECRAWL_SCRAPE"

    def none(slug, args):
        raise mcp.ComposioConnectionMissing(slug, mcp.toolkit_of(slug))

    fc = FakeClient(none)
    s = mcp.MCPScrape(client=fc)
    with pytest.raises(mcp.ComposioConnectionMissing):
        asyncio.run(s.fetch("https://c.dev"))
    assert cache_mod.get_cache().get(mcp.SCRAPE_TOOL, {"url": "https://c.dev"}) is None
    n = len(fc.calls)
    with pytest.raises(mcp.ComposioConnectionMissing):
        asyncio.run(s.fetch("https://d.dev"))
    assert len(fc.calls) == n  # unconnected toolkits are skipped after the first miss


def test_cache_keys_distinct_from_direct_backend():
    keys = {mcp.SEARCH_TOOL, mcp.SCRAPE_TOOL, "exa.search.v2", "httpx.scrape", "composio.search", "composio.scrape"}
    assert len(keys) == 6
    args = {"query": "q", "n": 2, "domains": []}
    assert cache_mod.make_key(mcp.SEARCH_TOOL, args) != cache_mod.make_key("exa.search.v2", args)


def test_inflight_cap(patch_settings):
    patch_settings(composio_max_inflight=2)
    state = {"now": 0, "max": 0}

    def h(req):
        body = json.loads(req.content)
        if body["method"] == "initialize":
            return httpx.Response(200, headers={"mcp-session-id": "s"}, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(200, json=tool_reply(body["id"], multi([{"response": {"successful": True, "data": 1}}])))

    class Slow(httpx.MockTransport):
        async def handle_async_request(self, request):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            await asyncio.sleep(0.01)
            state["now"] -= 1
            return await super().handle_async_request(request)

    c = mcp.ComposioMCPClient(api_key=KEY, url="https://mcp.test/mcp", transport=Slow(h))

    async def go():
        await c.call_tool("warm", {})
        state["max"] = 0
        await asyncio.gather(*(c.execute("EXA_SEARCH", {}) for _ in range(6)))

    asyncio.run(go())
    assert state["max"] <= 2


# ---------------------------------------------------------------- backend selection + fallback
def test_backend_autodetect_by_key_prefix(patch_settings):
    from agent.tools import composio_tools as ct, direct_tools as dt

    patch_settings(composio_api_key="ck_abc")
    s, sc, cat = base.get_tools("composio")
    assert isinstance(s, mcp.MCPSearch) and isinstance(sc, mcp.MCPScrape) and isinstance(cat, ct.DocsCatalog)
    assert cat.source == "composio_docs_catalog" and base.resolve_backend() == "composio_mcp"
    patch_settings(composio_api_key="ak_abc")
    s, sc, cat = base.get_tools("composio")
    assert isinstance(s, ct.ComposioSearch) and isinstance(cat, ct.FallbackCatalog)
    assert base.resolve_backend() == "composio_sdk"
    patch_settings(composio_api_key="ck_abc")
    assert isinstance(base.get_tools("composio_sdk")[0], ct.ComposioSearch)
    assert isinstance(base.get_tools("direct")[0], dt.ExaSearch)
    with pytest.raises(ValueError):
        base.get_tools("nope")


def test_fallback_only_with_env(patch_settings, monkeypatch):
    patch_settings(composio_api_key="ck_abc")
    assert not isinstance(base.get_tools("composio")[0], fb.FallbackSearch)
    monkeypatch.setenv("TOOL_FALLBACK", "something-else")
    assert not isinstance(base.get_tools("composio")[0], fb.FallbackSearch)
    monkeypatch.setenv("TOOL_FALLBACK", "direct")
    s, sc, _ = base.get_tools("composio")
    assert isinstance(s, fb.FallbackSearch) and isinstance(sc, fb.FallbackScrape) and s.counts is sc.counts
    assert not isinstance(base.get_tools("direct")[0], fb.FallbackSearch)


class _Search:
    def __init__(self, exc=None):
        self.exc, self.n = exc, 0

    async def search(self, query, n=8, include_domains=None):
        self.n += 1
        if self.exc:
            raise self.exc
        return [SearchResult(url="https://p", title="", snippet="")]


class _Scrape:
    def __init__(self, exc=None):
        self.exc, self.n = exc, 0

    async def fetch(self, url):
        self.n += 1
        if self.exc:
            raise self.exc
        return PageContent(url, "", "t", 200, "now", "x")


def test_fallback_counts_and_missing_connection_disables_primary(caplog):
    p, d = _Search(mcp.ComposioConnectionMissing("EXA_SEARCH", "exa")), _Search()
    ps, ds = _Scrape(), _Scrape()
    s, sc = fb.wrap(p, ps, d, ds)
    for _ in range(3):
        asyncio.run(s.search("q"))
    asyncio.run(sc.fetch("u"))
    assert p.n == 1 and d.n == 3  # primary never retried after a missing connection
    assert s.counts.snapshot() == {"fallbacks": 1, "direct": 3, "composio_mcp": 1}
    assert fb.backend_counts(s, sc) == s.counts.snapshot()
    assert sum("TOOL_FALLBACK=direct" in r.message for r in caplog.records) == 1


def test_fallback_transient_errors_then_disable():
    p, d = _Search(RuntimeError("boom")), _Search()
    s = fb.FallbackSearch(p, d, max_errors=2)
    for _ in range(4):
        asyncio.run(s.search("q"))
    assert p.n == 2 and d.n == 4 and s.counts.snapshot()["fallbacks"] == 2
    assert fb.backend_counts(_Search(), _Scrape()) is None


def test_transient_5xx_retried_then_typed_error(patch_settings, monkeypatch):
    n = {"calls": 0}

    def h(req):
        n["calls"] += 1
        return httpx.Response(503, text="down")

    c = mcp.ComposioMCPClient(api_key=KEY, url="https://mcp.test/mcp", transport=httpx.MockTransport(h))
    from tenacity import wait_none

    c.retry_wait = wait_none()  # no real backoff in tests
    with pytest.raises(mcp.ComposioMCPError):
        asyncio.run(c.call_tool("T", {}))
    assert n["calls"] == 3


def test_concurrent_expiry_never_sends_sessionless_call(patch_settings):
    """While one caller re-initialises, others must keep the stale id (-> 404 -> wait), never send none."""
    srv = FakeServer(lambda name, args: {"successful": True, "data": 1}, expire_once=True)
    c = client_for(srv)

    async def go():
        await c.call_tool("T", {})  # sess-1 established? (expire_once consumes it, re-inits to sess-2)
        srv.sessions = 0
        c.session_id = "sess-1"
        srv.expire_once = True
        return await asyncio.gather(*(c.call_tool("T", {}) for _ in range(6)))

    asyncio.run(go())
    calls = [r for r in srv.requests if json.loads(r.content)["method"] == "tools/call"]
    assert all(r.headers.get("mcp-session-id") for r in calls)
    assert srv.sessions == 1


def test_sse_keepalive_and_crlf():
    body = ': keepalive\r\n\r\nevent: message\r\ndata: {"jsonrpc":"2.0",\r\ndata: "id":1,"result":{}}\r\n\r\n'
    assert mcp.parse_rpc_response(body, "text/event-stream", 1)["id"] == 1
