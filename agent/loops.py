"""Verification loops L1-L4 (PRD FR-06). Results use the vocabulary pass | fail | skip | error."""
from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from rapidfuzz import fuzz

from agent.cache import DiskCache, get_cache
from agent.evidence import EvidenceIndex
from agent.registry import App, is_official_url
from agent.research import (OFFICIAL_TIERS, is_registry_claim, is_registry_evidence, normalize_url)
from agent.schema import AppRecord, Evidence, LoopResult

FUZZY_THRESHOLD = 90
L2_TIMEOUT_S = 15.0
L2_CACHE_TOOL = "l2.liveness"
BLOCKED = {401, 403, 429}
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CONCURRENCY = 8


def _r(loop: str, result: str, detail: str = "") -> LoopResult:
    return LoopResult(loop=loop, result=result, detail=detail)  # type: ignore[arg-type]


# =================================================================== L1
_OAUTH_RE = re.compile(r"oauth|authoriz", re.I)
_UNKNOWN = "unknown"


def _is_unknown(claim: Any) -> bool:
    return claim.is_unknown()


def l1_rules(record: AppRecord) -> dict[str, list[LoopResult]]:
    """Schema re-validation plus cross-field rules. Unknown claims get a single skip."""
    claims = record.claims
    out: dict[str, list[LoopResult]] = {}
    names = list(type(claims).model_fields)

    for name in names:
        claim = getattr(claims, name)
        if _is_unknown(claim):
            out[name] = [_r("L1", "skip", "unknown claim")]
            continue
        try:
            type(claim).model_validate(claim.model_dump())
            out[name] = [_r("L1", "pass", "schema: valid")]
        except Exception as e:  # noqa: BLE001 - pydantic ValidationError
            out[name] = [_r("L1", "fail", f"schema: {str(e).splitlines()[0][:150]}")]

    def add(name: str, ok: bool, rule: str, why: str = "") -> None:
        out[name].append(_r("L1", "pass" if ok else "fail", f"rule {rule}" + ("" if ok else f": {why}")))

    am = claims.auth_methods
    if not _is_unknown(am) and "oauth2" in am.value:
        ok = any(_OAUTH_RE.search(e.quote) for e in am.evidence)
        add("auth_methods", ok, "oauth2_quote", "oauth2 claimed but no quote mentions oauth/authoriz")

    ca, mb = claims.credential_access, claims.main_blocker
    if not _is_unknown(ca) and not _is_unknown(mb):
        gated = ca.value in ("partner_gated", "contact_sales")
        if gated and mb.value.type == "none":
            why = f"credential_access={ca.value} contradicts main_blocker type none"
            add("credential_access", False, "access_vs_blocker", why)
            add("main_blocker", False, "access_vs_blocker", why)
        else:
            add("credential_access", True, "access_vs_blocker")
            add("main_blocker", True, "access_vs_blocker")

    mo = claims.mcp_official
    if not _is_unknown(mo) and mo.value == "yes":
        ok = any(e.url.lower().startswith(("http://", "https://")) for e in mo.evidence)
        add("mcp_official", ok, "mcp_yes_has_url", "mcp_official=yes without an evidence URL")

    pg = claims.production_gate
    if not _is_unknown(pg) and pg.value != "none":
        ok = any(e.source_tier in OFFICIAL_TIERS for e in pg.evidence)
        add("production_gate", ok, "gate_official_evidence",
            f"production_gate={pg.value} without official-tier evidence")

    at = claims.api_type
    if not _is_unknown(at) and "graphql" in at.value:
        ok = any("graphql" in e.quote.lower() for e in at.evidence)
        add("api_type", ok, "graphql_quote", "graphql claimed but no quote mentions graphql")

    desc, docs = claims.description, claims.docs_url
    if not _is_unknown(docs):
        ok = not _is_unknown(desc) and bool(str(desc.value).strip())
        add("description", ok, "description_when_docs", "docs_url known but description empty/unknown")
    return out


# =================================================================== L2
async def _probe(http: httpx.AsyncClient, url: str) -> dict:
    """Return {"status": int} or {"error": str, "kind": "dns|timeout|invalid|other"}."""
    last: dict = {}
    headers = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
    for method in ("HEAD", "GET"):
        try:
            if method == "GET":
                async with http.stream("GET", url, follow_redirects=True, timeout=L2_TIMEOUT_S, headers=headers) as r:
                    status = r.status_code
            else:
                r = await http.request("HEAD", url, follow_redirects=True, timeout=L2_TIMEOUT_S, headers=headers)
                status = r.status_code
        except httpx.InvalidURL as e:
            return {"error": f"invalid url: {str(e)[:80]}", "kind": "invalid"}
        except httpx.TimeoutException as e:
            last = {"error": f"timeout {type(e).__name__}", "kind": "timeout"}
            continue
        except httpx.ConnectError as e:
            last = {"error": f"connect error: {str(e)[:80]}", "kind": "dns"}
            continue
        except httpx.HTTPError as e:
            last = {"error": f"{type(e).__name__}: {str(e)[:80]}", "kind": "other"}
            continue
        last = {"status": status}
        if status < 400:
            return last
    return last


async def liveness(url: str, http: httpx.AsyncClient, cache: Optional[DiskCache] = None,
                   memo: Optional[dict] = None) -> dict:
    cache = cache or get_cache()
    if memo is not None and url in memo:
        return await asyncio.shield(memo[url])

    async def go() -> dict:
        hit = cache.get(L2_CACHE_TOOL, {"url": url})
        if isinstance(hit, dict):
            return hit
        res = await _probe(http, url)
        # Statuses and DNS failures are facts about the URL; timeouts/other errors are transient.
        if "status" in res or res.get("kind") == "dns":
            cache.set(L2_CACHE_TOOL, {"url": url}, res)
        return res

    if memo is None:
        return await go()
    memo[url] = asyncio.ensure_future(go())
    return await asyncio.shield(memo[url])


async def l2_source(app: App, field: str, evidence: Evidence, http: httpx.AsyncClient, *,
                    cache: Optional[DiskCache] = None, memo: Optional[dict] = None) -> LoopResult:
    if is_registry_evidence(evidence):
        return _r("L2", "skip", "registry evidence")
    url = evidence.url
    if evidence.source_tier in OFFICIAL_TIERS and not is_official_url(app, url):
        return _r("L2", "fail", "tier claims official but domain not in official_domains")
    res = await liveness(url, http, cache, memo)
    if "status" in res:
        code = res["status"]
        if code < 400:
            return _r("L2", "pass", f"live {code}")
        if code in BLOCKED:
            return _r("L2", "pass", f"blocked {code}")
        if code >= 500:
            return _r("L2", "error", f"server error {code}")
        return _r("L2", "fail", f"dead {code}")
    kind = res.get("kind")
    if kind in ("dns", "invalid"):
        return _r("L2", "fail", res.get("error", "dns/connect error"))
    return _r("L2", "error", res.get("error", "request failed"))


# =================================================================== L3
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                         "“": '"', "”": '"', "„": '"', "″": '"'})
_MD_STRIP = re.compile(r"[|`*]")


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES)
    s = _MD_STRIP.sub(" ", s)
    return " ".join(s.split()).lower()


def _is_url(s: str) -> bool:
    return s.strip().lower().startswith(("http://", "https://")) and " " not in s.strip()


def _ground_text(field: str, evidence: Evidence, text: str) -> tuple[bool, str]:
    """Ground evidence.quote in text. Returns (ok, detail)."""
    q = evidence.quote
    if field == "docs_url" and _is_url(q):
        if normalize_url(q) == normalize_url(evidence.url):
            return True, "docs_url quote equals evidence url"
    nq, nt = normalise(q), normalise(text)
    if nq and nq in nt:
        return True, "exact"
    if nq and nt:
        score = fuzz.partial_ratio(nq, nt)
        if score >= FUZZY_THRESHOLD:
            return True, f"fuzzy {score:.0f}"
        return False, f"quote not in text (best fuzzy {score:.0f})"
    return False, "quote not in text"


async def l3_ground(field: str, evidence: Evidence, index: EvidenceIndex) -> tuple[LoopResult, bool]:
    """Returns (result, needs_render): True when page text was empty, or the quote is absent and the only
    text was an Exa search snippet (no full page)."""
    if is_registry_evidence(evidence):
        return _r("L3", "skip", "registry evidence"), False
    if field == "docs_url" and _is_url(evidence.quote) and \
            normalize_url(evidence.quote) == normalize_url(evidence.url):
        return _r("L3", "pass", "docs_url quote equals evidence url"), False
    text, via = await index.get_text(evidence.url)
    if not text.strip():
        return _r("L3", "fail", "no page text available (empty/blocked)"), True
    ok, detail = _ground_text(field, evidence, text)
    # A search snippet is only an excerpt of the page: a quote missing from it may still be on the full
    # page, so escalate to a browser render instead of concluding the quote is absent.
    snippet_only = via == "exa.snippet"
    return _r("L3", "pass" if ok else "fail", f"{detail} [{via}]"), (not ok and snippet_only)


# =================================================================== L4
async def l4_render_then_ground(field: str, evidence: Evidence, index: EvidenceIndex) -> LoopResult:
    try:
        page = await index.render(evidence.url)
    except Exception as e:  # noqa: BLE001
        return _r("L4", "error", f"render failed: {type(e).__name__}")
    if not (page.text or "").strip():
        return _r("L4", "error", f"render produced no text (status {page.status})")
    ok, detail = _ground_text(field, evidence, page.text)
    return _r("L4", "pass" if ok else "fail", f"{detail} [browser {page.status}]")


# ============================================================ orchestration
@dataclass
class EvidenceOutcome:
    url: str
    l2: LoopResult
    l3: LoopResult
    l4: Optional[LoopResult] = None

    @property
    def grounded(self) -> bool:
        return self.l3.result == "pass" or (self.l4 is not None and self.l4.result == "pass")

    @property
    def ok(self) -> bool:
        return self.l2.result in ("pass", "skip") and (self.l3.result in ("pass", "skip") or
                                                       (self.l4 is not None and self.l4.result == "pass"))


async def verify_evidence(app: App, field: str, ev: Evidence, index: EvidenceIndex,
                          http: httpx.AsyncClient, *, cache: Optional[DiskCache] = None,
                          memo: Optional[dict] = None) -> EvidenceOutcome:
    l2 = await l2_source(app, field, ev, http, cache=cache, memo=memo)
    l3, needs_render = await l3_ground(field, ev, index)
    l4 = None
    if needs_render and l2.result != "fail":
        l4 = await l4_render_then_ground(field, ev, index)
    return EvidenceOutcome(ev.url, l2, l3, l4)


def _short(url: str) -> str:
    p = urlparse(url)
    return (p.netloc + p.path)[:70]


def _aggregate(loop: str, outcomes: list[EvidenceOutcome], pick, need_l2: bool) -> Optional[LoopResult]:
    rows = [(o, pick(o)) for o in outcomes]
    rows = [(o, r) for o, r in rows if r is not None]
    if not rows:
        return None
    per = "; ".join(f"[{i}] {_short(o.url)}: {r.result} ({r.detail})" for i, (o, r) in enumerate(rows))
    results = [r.result for _, r in rows]
    if all(x == "skip" for x in results):
        return _r(loop, "skip", per)
    good = [o for o, r in rows if r.result == "pass" and (not need_l2 or o.l2.result != "fail")]
    if good:
        return _r(loop, "pass", per)
    if all(x == "error" for x in results):
        return _r(loop, "error", per)
    note = " (passes only on evidence failing L2)" if need_l2 and "pass" in results else ""
    return _r(loop, "fail", per + note)


async def run_l1_to_l4(record: AppRecord, app: App, index: EvidenceIndex,
                       http: httpx.AsyncClient, *, cache: Optional[DiskCache] = None) -> dict[str, list[LoopResult]]:
    """Per claim field: ordered [L1..., L2, L3, (L4)] with L2-L4 aggregated over the field's evidence.

    A field passes L2/L3 if at least one evidence item passes both (per-evidence detail in `detail`).
    """
    out = l1_rules(record)
    sem = asyncio.Semaphore(CONCURRENCY)
    memo: dict = {}

    async def one(field: str, ev: Evidence) -> EvidenceOutcome:
        async with sem:
            return await verify_evidence(app, field, ev, index, http, cache=cache, memo=memo)

    jobs: dict[str, list[asyncio.Task]] = {}
    for name in type(record.claims).model_fields:
        claim = getattr(record.claims, name)
        if claim.is_unknown():
            continue
        if is_registry_claim(claim):
            out[name] += [_r("L2", "skip", "registry claim"), _r("L3", "skip", "registry claim")]
            continue
        jobs[name] = [asyncio.ensure_future(one(name, ev)) for ev in claim.evidence]
    for name, tasks in jobs.items():
        outcomes = await asyncio.gather(*tasks)
        for loop, pick, need in (("L2", lambda o: o.l2, False), ("L3", lambda o: o.l3, True),
                                 ("L4", lambda o: o.l4, True)):
            agg = _aggregate(loop, list(outcomes), pick, need)
            if agg is not None:
                out[name].append(agg)
    return out
