"""Composio Connect MCP backend (consumer keys, prefix ``ck_``).

Protocol facts (verified live 2026-11-23):
- ``POST https://connect.composio.dev/mcp``, MCP streamable HTTP (JSON-RPC 2.0). Responses are either
  ``application/json`` or SSE (``data: {...}`` lines).
- Auth header ``x-consumer-api-key: <ck_...>``. The Composio SDK/REST rejects consumer keys (401).
- Flow: ``initialize`` -> read the ``mcp-session-id`` response header and send it on every later
  request -> ``notifications/initialized`` -> ``tools/call``.
- ``tools/call`` result: ``result.content[0].text`` is JSON ``{"successful", "data", "error"}``.
- Toolkit tools (EXA_SEARCH, ...) run through the meta tool ``COMPOSIO_MULTI_EXECUTE_TOOL``.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import weakref
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from agent.cache import get_cache
from agent.config import COMPOSIO_TOOL_SLUGS, get_settings, require
from agent.tools.base import PageContent, SearchResult
from agent.tools.composio_tools import parse_scrape, parse_search

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
MULTI_EXECUTE = "COMPOSIO_MULTI_EXECUTE_TOOL"
MANAGE_CONNECTIONS = "COMPOSIO_MANAGE_CONNECTIONS"
SNIPPET_CHARS = 8000
_KEY_RE = re.compile(r"\b(?:ck|ak)_[A-Za-z0-9_\-]{6,}")
_MISSING_RE = re.compile(r"no active connection", re.I)


# ------------------------------------------------------------------ errors
class ComposioMCPError(RuntimeError):
    """Transport/protocol failure talking to the MCP server (message is key-scrubbed)."""


class ComposioToolError(RuntimeError):
    """A Composio tool ran but reported failure."""

    def __init__(self, tool_slug: str, message: str) -> None:
        self.tool_slug = tool_slug
        self.message = scrub(message)[:500]
        super().__init__(f"{tool_slug} failed: {self.message}")


class ComposioConnectionMissing(ComposioToolError):
    """The toolkit has no active connection for this consumer key (run `agent composio connect`)."""

    def __init__(self, tool_slug: str, toolkit: str, message: str = "") -> None:
        self.toolkit = toolkit
        super().__init__(tool_slug, message or f"no active connection for toolkit {toolkit!r}")


class ComposioUpstreamAuthError(ComposioToolError):
    """The toolkit connection exists but the upstream API rejected its stored credential (401/403).
    Fix by reconnecting the toolkit with a valid key (`agent composio connect <toolkit>`)."""

    def __init__(self, tool_slug: str, toolkit: str, status_code: int, message: str = "") -> None:
        self.toolkit, self.status_code = toolkit, status_code
        super().__init__(tool_slug, f"upstream {status_code} for toolkit {toolkit!r}: {message}")


class _SessionExpired(Exception):
    pass


class _Transient(Exception):
    """Retryable server answer (429 / 5xx)."""


def scrub(msg: object) -> str:
    """Remove the configured Composio key and anything shaped like a Composio key."""
    text = str(msg)
    key = get_settings().composio_api_key
    if key and len(key) >= 6:
        text = text.replace(key, "[REDACTED]")
    return _KEY_RE.sub("[REDACTED]", text)


def toolkit_of(tool_slug: str) -> str:
    """EXA_SEARCH -> exa, FIRECRAWL_SCRAPE -> firecrawl."""
    return tool_slug.split("_", 1)[0].lower()


# ------------------------------------------------------------------ parsing
def parse_sse(text: str) -> list[dict]:
    """JSON payloads of every SSE event in `text` (multi-line `data:` fields are joined)."""
    events: list[dict] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            try:
                obj = json.loads("\n".join(buf))
                if isinstance(obj, dict):
                    events.append(obj)
            except ValueError:
                pass
            buf.clear()

    for line in text.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            buf.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
    flush()
    return events


def parse_rpc_response(text: str, content_type: str, req_id: Optional[int]) -> dict:
    """Return the JSON-RPC response object for `req_id` from a JSON or SSE body."""
    if "text/event-stream" in content_type.lower() or text.lstrip().startswith(("data:", "event:", ":")):
        msgs = parse_sse(text)
    else:
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise ComposioMCPError(f"non-JSON MCP response: {scrub(text[:200])}") from e
        msgs = obj if isinstance(obj, list) else [obj]
    matches = [m for m in msgs if isinstance(m, dict) and ("result" in m or "error" in m)]
    for m in matches:
        if req_id is None or m.get("id") == req_id:
            return m
    if matches:
        return matches[-1]
    raise ComposioMCPError(f"no JSON-RPC response in MCP body: {scrub(text[:200])}")


def parse_tool_result(rpc: dict) -> dict:
    """`result.content[0].text` (JSON) of a tools/call response, as a dict."""
    if "error" in rpc:
        err = rpc["error"]
        msg = err.get("message") if isinstance(err, dict) else err
        raise ComposioMCPError(f"MCP error: {scrub(msg)[:300]}")
    result = rpc.get("result") or {}
    content = result.get("content") or []
    text = next((c.get("text") for c in content if isinstance(c, dict) and c.get("type", "text") == "text"), None)
    if text is None:
        return {"successful": not result.get("isError", False), "data": result.get("structuredContent"),
                "error": None}
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = {"successful": not result.get("isError", False), "data": {"text": text},
                  "error": text if result.get("isError") else None}
    if not isinstance(parsed, dict):
        parsed = {"successful": True, "data": parsed, "error": None}
    if result.get("isError") and "successful" not in parsed:
        parsed["successful"] = False
    return parsed


def _err_text(err: Any) -> str:
    if err is None:
        return ""
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        return str(err.get("message") or err.get("error") or json.dumps(err, default=str))
    return str(err)


def _raise_for(tool_slug: str, err: Any, status_code: Any = None) -> None:
    msg = _err_text(err) or "unknown error"
    if _MISSING_RE.search(msg):
        raise ComposioConnectionMissing(tool_slug, toolkit_of(tool_slug), msg)
    if status_code in (401, 403):
        raise ComposioUpstreamAuthError(tool_slug, toolkit_of(tool_slug), status_code, msg)
    raise ComposioToolError(tool_slug, msg)


def _status_code(resp: Any) -> Any:
    if isinstance(resp, dict):
        d = resp.get("data")
        if isinstance(d, dict) and isinstance(d.get("status_code"), int):
            return d["status_code"]
        if isinstance(resp.get("status_code"), int):
            return resp["status_code"]
    return None


def parse_multi_execute(tool_slug: str, outer: dict) -> Any:
    """Return the per-tool `data` of a COMPOSIO_MULTI_EXECUTE_TOOL answer, or raise a typed error."""
    if not outer.get("successful", True) and not (outer.get("data") or {}).get("results"):
        _raise_for(tool_slug, outer.get("error"))
    data = outer.get("data") or {}
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        if outer.get("error"):
            _raise_for(tool_slug, outer.get("error"))
        raise ComposioToolError(tool_slug, "multi-execute returned no results")
    item = next((r for r in results if isinstance(r, dict) and r.get("tool_slug") == tool_slug), results[0])
    if not isinstance(item, dict):
        return item
    resp = item.get("response", item)
    if item.get("error"):
        _raise_for(tool_slug, item["error"], _status_code(resp))
    if isinstance(resp, dict) and "successful" in resp:
        if not resp.get("successful"):
            _raise_for(tool_slug, resp.get("error"), _status_code(resp))
        return resp.get("data")
    return resp.get("data", resp) if isinstance(resp, dict) else resp


# ------------------------------------------------------------------ client
def _retryable(e: BaseException) -> bool:
    return isinstance(e, (_Transient, httpx.TransportError))


class ComposioMCPClient:
    """Async client for the Composio Connect MCP server. One per event loop (see `get_mcp_client`)."""

    def __init__(self, api_key: Optional[str] = None, url: Optional[str] = None,
                 timeout: float = 120.0, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        s = get_settings()
        self._key = api_key or s.composio_api_key or require("COMPOSIO_API_KEY")
        self.url = url or s.composio_mcp_url
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0), transport=transport,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        )
        self.session_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self.inits = 0
        self.retry_wait = wait_exponential(multiplier=1, min=1, max=8)

    def _scrub(self, msg: object) -> str:
        return scrub(str(msg).replace(self._key, "[REDACTED]"))

    def _headers(self, session_id: Optional[str]) -> dict[str, str]:
        h = {"x-consumer-api-key": self._key}
        if session_id:
            h["mcp-session-id"] = session_id
        return h

    async def _post(self, payload: dict, session_id: Optional[str] = "__current__") -> httpx.Response:
        """POST one JSON-RPC message. `session_id` defaults to the current session; pass None/explicit id
        during initialise so concurrent callers never observe a half-initialised (cleared) session."""
        sid = self.session_id if session_id == "__current__" else session_id
        try:
            r = await self._http.post(self.url, json=payload, headers=self._headers(sid))
        except httpx.TransportError as e:
            raise type(e)(self._scrub(e)) from None
        if r.status_code == 404 and sid:
            raise _SessionExpired()
        if r.status_code == 429 or r.status_code >= 500:
            raise _Transient(f"HTTP {r.status_code}")
        if r.status_code >= 400:
            body = r.text[:300]
            if sid and re.search(r"session", body, re.I):
                raise _SessionExpired()
            raise ComposioMCPError(f"HTTP {r.status_code}: {self._scrub(body)}")
        return r

    async def _initialize(self) -> None:
        rid = next(self._ids)
        r = await self._post({"jsonrpc": "2.0", "id": rid, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "app-intel-agent", "version": "0.1"}}}, session_id=None)
        resp = parse_rpc_response(r.text, r.headers.get("content-type", ""), rid)
        if "error" in resp:
            raise ComposioMCPError(f"initialize failed: {self._scrub(resp['error'])[:300]}")
        new_sid = r.headers.get("mcp-session-id")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=new_sid)
        self.session_id = new_sid  # published only once fully initialised
        self.inits += 1

    async def _ensure_session(self, stale: Optional[str] = "__none__") -> None:
        async with self._lock:  # concurrent callers share one initialize
            if stale != "__none__" and self.session_id != stale:
                return  # someone else already re-initialised
            if stale == "__none__" and self.inits:
                return
            await self._initialize()

    async def _rpc_once(self, method: str, params: dict) -> dict:
        await self._ensure_session()
        for _ in range(2):  # one re-initialise on session expiry
            sid = self.session_id
            rid = next(self._ids)
            try:
                r = await self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            except _SessionExpired:
                log.info("composio MCP session expired; re-initialising")
                await self._ensure_session(stale=sid)
                continue
            return parse_rpc_response(r.text, r.headers.get("content-type", ""), rid)
        raise ComposioMCPError("MCP session could not be re-established")

    async def rpc(self, method: str, params: dict) -> dict:
        try:
            async for attempt in AsyncRetrying(stop=stop_after_attempt(3),
                                               wait=self.retry_wait,
                                               retry=retry_if_exception(_retryable), reraise=True):
                with attempt:
                    return await self._rpc_once(method, params)
        except _Transient as e:
            raise ComposioMCPError(f"MCP server unavailable after retries ({e})") from None
        raise AssertionError("unreachable")

    async def call_tool(self, name: str, args: dict) -> dict:
        """Call an MCP tool; return the parsed inner JSON (`{"successful","data","error"}`)."""
        return parse_tool_result(await self.rpc("tools/call", {"name": name, "arguments": args}))

    async def list_tools(self) -> list[dict]:
        return (await self.rpc("tools/list", {})).get("result", {}).get("tools", [])

    async def execute(self, tool_slug: str, arguments: dict) -> Any:
        """Run one toolkit tool via COMPOSIO_MULTI_EXECUTE_TOOL and return its `data`."""
        async with _inflight():
            outer = await self.call_tool(MULTI_EXECUTE, {
                "tools": [{"tool_slug": tool_slug, "arguments": arguments}],
                "sync_response_to_workbench": False})
        return parse_multi_execute(tool_slug, outer)

    async def tool_schemas(self, slugs: list[str]) -> dict:
        return await self.call_tool("COMPOSIO_GET_TOOL_SCHEMAS", {"tool_slugs": slugs})

    async def connection_status(self, toolkits: list[str]) -> dict[str, dict]:
        """{toolkit: {"active": bool, "status": str, ...}} via COMPOSIO_MANAGE_CONNECTIONS (list)."""
        out = await self.call_tool(MANAGE_CONNECTIONS,
                                   {"toolkits": [{"name": t, "action": "list"} for t in toolkits]})
        return parse_connection_status(out, toolkits)

    async def initiate_connection(self, toolkit: str) -> Optional[str]:
        """Start an auth flow for `toolkit`; return the redirect URL (expires in ~10 min), if any."""
        out = await self.call_tool(MANAGE_CONNECTIONS, {"toolkits": [{"name": toolkit, "action": "add"}]})
        if not out.get("successful", True):
            raise ComposioToolError(MANAGE_CONNECTIONS, _err_text(out.get("error")))
        return _find_url(out.get("data"))

    async def aclose(self) -> None:
        await self._http.aclose()


def _find_url(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for k in ("redirect_url", "redirectUrl", "url", "auth_url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            r = _find_url(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_url(v)
            if r:
                return r
    return None


_ACTIVE = {"active", "connected"}


def parse_connection_status(out: dict, toolkits: list[str]) -> dict[str, dict]:
    """Best-effort normalisation of COMPOSIO_MANAGE_CONNECTIONS output to per-toolkit status."""
    data = out.get("data") or {}
    res: dict[str, dict] = {t: {"active": False, "status": "unknown"} for t in toolkits}
    blob = data.get("results") or data.get("toolkits") or data.get("connections") or data
    items: list[tuple[str, Any]] = []
    if isinstance(blob, dict):
        items = [(k.lower(), v) for k, v in blob.items()]
    elif isinstance(blob, list):
        for v in blob:
            if isinstance(v, dict):
                name = v.get("toolkit") or v.get("toolkit_slug") or v.get("slug") or v.get("name") or ""
                if isinstance(name, dict):
                    name = name.get("slug") or ""
                items.append((str(name).lower(), v))
    for name, v in items:
        if name not in res:
            continue
        status = "unknown"
        if isinstance(v, dict):
            status = str(v.get("status") or v.get("connection_status") or
                         ("active" if v.get("has_active_connection") or v.get("is_active") else
                          "not_connected" if v.get("has_active_connection") is False else "unknown"))
            accounts = v.get("accounts") if isinstance(v.get("accounts"), list) else []
            active = status.lower() in _ACTIVE or any(
                isinstance(a, dict) and str(a.get("status", "")).lower() in _ACTIVE for a in accounts)
            res[name] = {"active": active, "status": status, "accounts": len(accounts)}
        elif isinstance(v, str):
            res[name] = {"active": v.lower() in _ACTIVE, "status": v}
    return res


# ------------------------------------------------------------------ per-loop singletons
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, ComposioMCPClient]" = weakref.WeakKeyDictionary()
_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = weakref.WeakKeyDictionary()


def get_mcp_client() -> ComposioMCPClient:
    """The shared client for the running event loop (httpx/asyncio objects are loop-bound)."""
    loop = asyncio.get_running_loop()
    c = _clients.get(loop)
    if c is None:
        c = _clients[loop] = ComposioMCPClient()
    return c


def _inflight() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = _sems[loop] = asyncio.Semaphore(get_settings().composio_max_inflight)
    return sem


# ------------------------------------------------------------------ tools
SEARCH_TOOL = "composio_mcp.exa_search"
SCRAPE_TOOL = "composio_mcp.scrape"


class MCPSearch:
    """Web search via EXA_SEARCH on the Composio Connect MCP server."""

    backend = "composio_mcp"

    def __init__(self, client: Optional[ComposioMCPClient] = None) -> None:
        self._client = client

    def _c(self) -> ComposioMCPClient:
        return self._client or get_mcp_client()

    @staticmethod
    def exa_args(query: str, n: int, include_domains: list[str] | None) -> dict:
        # Schema (COMPOSIO_GET_TOOL_SCHEMAS, 2026-11-23): query, numResults, includeDomains,
        # contents.text = bool | {maxCharacters, verbosity, ...}.
        args: dict[str, Any] = {"query": query, "numResults": n,
                                "contents": {"text": {"maxCharacters": SNIPPET_CHARS}}}
        if include_domains:
            args["includeDomains"] = list(include_domains)
        return args

    async def search(self, query: str, n: int = 8, include_domains: list[str] | None = None) -> list[SearchResult]:
        key = {"query": query, "n": n, "domains": sorted(include_domains or [])}
        cache = get_cache()
        hit = cache.get(SEARCH_TOOL, key)
        if hit is not None:
            return [SearchResult(**r) for r in hit]
        slug = COMPOSIO_TOOL_SLUGS["search"] or "EXA_SEARCH"
        data = await self._c().execute(slug, self.exa_args(query, n, include_domains))
        out = parse_exa_results(data, n)
        # A successful empty answer is a real answer (cache it, like the direct backend); errors raise above.
        cache.set(SEARCH_TOOL, key, [r.to_dict() for r in out])
        return out


def parse_exa_results(data: Any, n: int) -> list[SearchResult]:
    """EXA_SEARCH data -> SearchResult, preferring full page `text` (<= 8000 chars) as the snippet."""
    base = parse_search(data, n)
    from agent.tools.composio_tools import _find

    items = _find(data, lambda o: isinstance(o, list) and o and all(isinstance(x, dict) and "url" in x for x in o)) or []
    by_url = {it["url"]: it for it in items}
    for r in base:
        text = (by_url.get(r.url) or {}).get("text")
        if isinstance(text, str) and text:
            r.snippet = text[:SNIPPET_CHARS]
        else:
            r.snippet = (r.snippet or "")[:SNIPPET_CHARS]
    return base


class MCPScrape:
    """Page fetch via EXA_GET_CONTENTS_ACTION (primary) and FIRECRAWL_SCRAPE (secondary, if connected)."""

    backend = "composio_mcp"

    def __init__(self, client: Optional[ComposioMCPClient] = None) -> None:
        self._client = client
        # toolkit -> the non-transient error that made us stop asking it (missing connection / upstream 401)
        self._skip: dict[str, ComposioToolError] = {}

    def _c(self) -> ComposioMCPClient:
        return self._client or get_mcp_client()

    async def fetch(self, url: str) -> PageContent:
        key = {"url": url}
        cache = get_cache()
        hit = cache.get(SCRAPE_TOOL, key)
        if hit is not None:
            return PageContent(**hit)
        attempts = [
            ("EXA_GET_CONTENTS_ACTION", {"urls": [url], "text": {"maxCharacters": 100_000}}),
            ("FIRECRAWL_SCRAPE", {"url": url, "formats": ["markdown"], "onlyMainContent": True}),
        ]
        last_err: Optional[Exception] = None
        for slug, args in attempts:
            if toolkit_of(slug) in self._skip:
                continue
            try:
                data = await self._c().execute(slug, args)
            except (ComposioConnectionMissing, ComposioUpstreamAuthError) as e:
                self._skip[e.toolkit] = e
                continue
            except ComposioToolError as e:
                last_err = e
                continue
            page = parse_scrape(_pick_url(data, url), url, f"composio_mcp:{slug}")
            if page.text:
                cache.set(SCRAPE_TOOL, key, page.to_dict())
                return page
        if last_err is not None:
            raise last_err
        if len(self._skip) >= len(attempts):  # no usable scrape toolkit at all: surface it (caller decides)
            raise next(iter(self._skip.values()))
        return PageContent(url=url, title="", text="", status=0,
                           fetched_at=datetime.now(timezone.utc).isoformat(), via="composio_mcp")


def _pick_url(data: Any, url: str) -> Any:
    """For multi-URL results lists, narrow to the entry for `url` (so the title/text match)."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        for it in data["results"]:
            if isinstance(it, dict) and it.get("url") in (url, url.rstrip("/"), url + "/"):
                return it
    return data
