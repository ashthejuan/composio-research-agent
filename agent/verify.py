"""Phase 3 verification (PRD FR-06): L1-L4 (agent.loops), L5 independent verifier, L6 targeted re-research.

Ledger vocabulary (attribution/loopstats depend on it):
    L1-L4: pass | fail | skip | error
    L5:    supported | partially_supported | contradicted | insufficient_evidence | error
    L6:    changed | confirmed | set_unknown | no_change | error
           changed/set_unknown detail = "<old value json> -> <new value json>[ | reason]"

Public interface used by the orchestrator:
    async def verify_app(record, app, tools) -> AppRecord
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict
from rapidfuzz import fuzz

from agent import evidence as evidence_mod  # module refs so tests can monkeypatch
from agent import llm, loops
from agent import research as R
from agent.config import get_settings
from agent.prompts import (SYSTEM_PROMPT, VERIFIER_FIELD_DEFS, Extraction, build_field_prompt,
                           build_verifier_prompt)
from agent.registry import App
from agent.schema import UNKNOWN, AppRecord, Blocker, Claim, Evidence, LoopResult

# ------------------------------------------------------------------ constants
EXCERPT_WINDOW = 1200  # chars either side of the quote match shown to the verifier
MAX_EVIDENCE_PER_CLAIM = 3
L5_CHUNK_CHARS = 24_000  # split an app's verifier prompt above this size
REASON_CHARS = 200
L6_FIELD_CONCURRENCY = 3
L6_MAX_ATTEMPTS = 2
L6_NEW_SOURCES = 4
SET_FIELDS = ("auth_methods", "api_type", "official_sdks")
L5_STATUSES = ("supported", "partially_supported", "contradicted", "insufficient_evidence")

# Two attempts per field, each with its own phrasing (generic templates; {s} = research subject).
L6_QUERIES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "auth_methods": (
        ("{s} API authentication methods OAuth API key token", "{s} API authorization guide authenticate requests"),
        ("{s} developer docs access token OAuth 2.0 client credentials",
         "{s} API key personal access token how to authenticate"),
    ),
    "credential_access": (
        ("{s} API access pricing plan free developer account", "{s} which plans include API access"),
        ("{s} get API key sign up developer free trial", "{s} API access enterprise plan contact sales requirement"),
    ),
    "production_gate": (
        ("{s} public app review OR marketplace listing requirements for third-party integrations",
         "{s} partner program API access"),
        ("{s} publish OAuth app verification approval before customers can install",
         "{s} app directory submission guidelines integration"),
    ),
    "api_type": (
        ("{s} API reference REST GraphQL", "{s} API endpoints developer documentation"),
        ("{s} REST API GraphQL API SDK overview", "{s} API base URL HTTP requests JSON"),
    ),
    "mcp_official": (
        ("{s} official MCP server Model Context Protocol", "{s} MCP server connect AI assistant"),
        ("{s} remote MCP server documentation", "{s} Model Context Protocol integration github"),
    ),
    "main_blocker": (
        ("{s} API limitations restrictions access requirements",),
        ("{s} API terms of use restrictions approval required",),
    ),
}

# Keywords for the fallback excerpt window when a quote is not found verbatim (generic, per field).
FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "description": (),
    "auth_methods": ("oauth", "api key", "token", "authenticat", "bearer", "basic auth", "authoriz"),
    "credential_access": ("pricing", "plan", "free", "trial", "sign up", "developer account", "enterprise",
                          "contact sales", "api access"),
    "production_gate": ("app review", "review", "marketplace", "partner", "publish", "listing", "verif",
                        "approv"),
    "api_type": ("rest", "graphql", "soap", "websocket", "grpc", "rpc", "endpoint", "sdk", "cli"),
    "api_breadth": ("endpoint", "resource", "reference", "object"),
    "mcp_official": ("mcp", "model context protocol"),
    "mcp_third_party": ("mcp", "model context protocol"),
    "free_tier_or_sandbox": ("free", "sandbox", "trial", "test mode", "developer account"),
    "webhooks": ("webhook", "event", "subscription"),
    "official_sdks": ("sdk", "library", "libraries", "client"),
    "rate_limit_note": ("rate limit", "requests per", "throttl", "429"),
    "open_source_self_host": ("open source", "open-source", "self-host", "license"),
    "main_blocker": ("partner", "review", "approv", "enterprise", "contact sales", "paid", "limit", "restrict"),
    "docs_url": ("api", "reference", "documentation"),
}


def _r(loop: str, result: str, detail: str = "") -> LoopResult:
    return LoopResult(loop=loop, result=result, detail=detail)  # type: ignore[arg-type]


def value_json(v: Any) -> str:
    """Ledger rendering of a claim value (Blocker as its dict)."""
    if isinstance(v, BaseModel):
        v = v.model_dump()
    return json.dumps(v, ensure_ascii=False)


def _unknown_value(name: str) -> Any:
    if name in SET_FIELDS:
        return [UNKNOWN]
    if name == "main_blocker":
        return Blocker(text=UNKNOWN, type="unknown")
    return UNKNOWN


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(str, a)) == sorted(map(str, b))
    if isinstance(a, Blocker) and isinstance(b, Blocker):
        return a.type == b.type and a.text.strip().lower() == b.text.strip().lower()
    return a == b


# ------------------------------------------------------------------ verifier models
class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    status: Literal["supported", "partially_supported", "contradicted", "insufficient_evidence"]
    reason: str


class VerdictList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[Verdict]


_verifier_sems: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = \
    weakref.WeakKeyDictionary()


def _verifier_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _verifier_sems.get(loop)
    if sem is None:
        n = max(1, int(os.environ.get("VERIFIER_MAX_INFLIGHT", "") or 4))
        sem = _verifier_sems[loop] = asyncio.Semaphore(n)
    return sem


def is_fatal(e: BaseException) -> bool:
    """Errors no retry inside this app can fix (missing key, bad key, unknown model)."""
    if isinstance(e, RuntimeError) and "Missing required environment variable" in str(e):
        return True
    try:
        from openai import AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError

        if isinstance(e, BadRequestError):
            msg = str(e).lower()
            return "model" in msg and ("not found" in msg or "does not exist" in msg or "invalid" in msg)
        return isinstance(e, (AuthenticationError, PermissionDeniedError, NotFoundError))
    except ImportError:  # pragma: no cover
        return False


# ------------------------------------------------------------------ excerpts
def locate_quote(quote: str, text: str) -> Optional[tuple[int, int]]:
    """(start, end) of the quote in text: exact (case-insensitive), word-sequence, then fuzzy >= threshold."""
    q = (quote or "").strip().lower()
    if not q or not text:
        return None
    low = text.lower()
    i = low.find(q)
    if i >= 0:
        return i, i + len(q)
    words = re.findall(r"\w+", q)
    if len(words) >= 3:
        m = re.search(r"\W*".join(map(re.escape, words)), low)
        if m:
            return m.start(), m.end()
    al = fuzz.partial_ratio_alignment(q, low, score_cutoff=loops.FUZZY_THRESHOLD)
    if al is not None and al.dest_end > al.dest_start:
        return al.dest_start, al.dest_end
    return None


def _snap(text: str, a: int, b: int) -> tuple[int, int]:
    if a > 0:
        sp = text.find(" ", a, min(len(text), a + 40))
        a = sp + 1 if sp >= 0 else a
    if b < len(text):
        sp = text.rfind(" ", max(a, b - 40), b)
        b = sp if sp > a else b
    return a, b


def keyword_window(name: str, text: str, window: int = EXCERPT_WINDOW) -> tuple[int, int]:
    """Window of 2*window chars with the most distinct field-keyword hits (page intro if none)."""
    low = text.lower()
    hits: list[tuple[int, str]] = []
    for kw in FIELD_KEYWORDS.get(name, ()):
        pos = 0
        while len(hits) < 400:
            i = low.find(kw, pos)
            if i < 0:
                break
            hits.append((i, kw))
            pos = i + len(kw)
    if not hits:
        return 0, min(len(text), 2 * window)
    best, best_score = hits[0][0], -1
    for p, _ in hits:
        score = len({kw for q, kw in hits if p - window <= q <= p + window})
        if score > best_score:
            best, best_score = p, score
    return max(0, best - window), min(len(text), best + window)


def excerpt_for(name: str, ev: Evidence, text: str, *, mark: bool = True) -> tuple[str, bool]:
    """(excerpt, quote_found): +/-EXCERPT_WINDOW chars around the quote, else the best keyword window."""
    span = locate_quote(ev.quote, text)
    if span is not None:
        a, b = span
        lo, hi = _snap(text, max(0, a - EXCERPT_WINDOW), min(len(text), b + EXCERPT_WINDOW))
        lo, hi = min(lo, a), max(hi, b)
        if mark:
            return f"{text[lo:a]}>>>{text[a:b]}<<<{text[b:hi]}".strip(), True
        return text[lo:hi].strip(), True
    lo, hi = keyword_window(name, text)
    lo, hi = _snap(text, lo, hi)
    return text[lo:hi].strip(), False


async def _evidence_text(index: Any, url: str) -> str:
    text, _ = await index.get_text(url)
    rendered = index.rendered_text(url) if hasattr(index, "rendered_text") else ""
    if rendered and len(rendered) > len(text or ""):
        return rendered
    return text or ""


async def _excerpt(index: Any, name: str, ev: Evidence, mark: bool = True) -> tuple[str, bool]:
    text = await _evidence_text(index, ev.url)
    if text.strip():
        span_text = text
        if locate_quote(ev.quote, text) is None and hasattr(index, "rendered_text"):
            r = index.rendered_text(ev.url)  # quote may only be on the rendered page
            if r and locate_quote(ev.quote, r) is not None:
                span_text = r
        return excerpt_for(name, ev, span_text, mark=mark)
    return "", False


async def claim_block(index: Any, name: str, claim: Claim) -> str:
    lines = [f"## field: {name}", f"Definition: {VERIFIER_FIELD_DEFS.get(name, '')}",
             f"Claimed value: {value_json(claim.value)}"]
    for i, ev in enumerate(claim.evidence[:MAX_EVIDENCE_PER_CLAIM], 1):
        ex, found = await _excerpt(index, name, ev)
        if not ex:
            lines.append(f"Excerpt {i} ({ev.url}): (no page text available)")
            continue
        how = "quote found" if found else "quote not found verbatim; best keyword window"
        lines.append(f'Excerpt {i} ({ev.url}; {how}):\n"""\n{ex}\n"""')
    return "\n".join(lines)


# ------------------------------------------------------------------ L5
@dataclass
class _Usage:
    verifier_in: int = 0
    verifier_out: int = 0
    extract_in: int = 0
    extract_out: int = 0

    def add_verifier(self, u: dict) -> None:
        self.verifier_in += int(u.get("input_tokens", 0) or 0)
        self.verifier_out += int(u.get("output_tokens", 0) or 0)

    def add_extract(self, u: dict) -> None:
        self.extract_in += int(u.get("input_tokens", 0) or 0)
        self.extract_out += int(u.get("output_tokens", 0) or 0)


def _chunks(items: list[tuple[str, str]], limit: Optional[int] = None) -> list[list[tuple[str, str]]]:
    limit = L5_CHUNK_CHARS if limit is None else limit
    out: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    size = 0
    for it in items:
        if cur and size + len(it[1]) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it[1])
    if cur:
        out.append(cur)
    return out


def _norm_field(s: str) -> str:
    return (s or "").strip().strip("`'\"").lower()


async def run_l5(app_name: str, items: list[tuple[str, str]], usage: _Usage,
                 errors: list[str]) -> tuple[dict[str, LoopResult], int]:
    """items = [(field, claim block)]. Returns ({field: L5 result}, number of failed verifier calls)."""
    results: dict[str, LoopResult] = {}
    failed_calls = 0

    async def call(chunk: list[tuple[str, str]]) -> None:
        nonlocal failed_calls
        prompt = build_verifier_prompt(app_name, [b for _, b in chunk])
        fields = [f for f, _ in chunk]
        try:
            async with _verifier_sem():
                parsed, u = await llm.verify_claim(prompt, VerdictList)
        except Exception as e:  # noqa: BLE001
            if is_fatal(e):
                raise
            failed_calls += 1
            errors.append(f"verify: L5 call failed for {len(fields)} claims ({type(e).__name__})")
            for f in fields:
                results[f] = _r("L5", "error", f"verifier call failed: {type(e).__name__}")
            return
        usage.add_verifier(u or {})
        if not isinstance(parsed, VerdictList):
            try:
                parsed = VerdictList.model_validate(parsed)
            except Exception:  # noqa: BLE001
                parsed = VerdictList(verdicts=[])
        got: dict[str, Verdict] = {}
        for v in parsed.verdicts:
            k = _norm_field(v.field)
            if k in fields and k not in got and v.status in L5_STATUSES:
                got[k] = v
        missing = [f for f in fields if f not in got]
        if missing:
            errors.append(f"verify: L5 returned no verdict for {', '.join(missing)}")
        for f in fields:
            v = got.get(f)
            results[f] = (_r("L5", v.status, " ".join(v.reason.split())[:REASON_CHARS]) if v is not None
                          else _r("L5", "error", "verifier returned no verdict for this claim"))

    await asyncio.gather(*(call(c) for c in _chunks(items)))
    return results, failed_calls


def _grounded(entries: list[LoopResult]) -> bool:
    return any(e.loop in ("L3", "L4") and e.result == "pass" for e in entries)


def _has_l1_fail(entries: list[LoopResult]) -> bool:
    return any(e.loop == "L1" and e.result == "fail" for e in entries)


def pre_l6_status(l5: str, grounded: bool, l1_failed: bool = False) -> tuple[str, str]:
    """(status, note) from the L5 verdict, L3/L4 grounding, and L1 cross-field rules."""
    if l5 == "supported":
        if l1_failed:
            return "insufficient_evidence", "verifier said supported but claim failed an L1 rule"
        if grounded:
            return "supported", ""
        return "insufficient_evidence", "verifier said supported but no quote was grounded by L3/L4"
    if l5 == "error":
        return "insufficient_evidence", "verifier error: no verdict"
    return l5, ""


# ------------------------------------------------------------------ context
@dataclass
class _Ctx:
    app: App
    record: AppRecord
    index: Any
    http: httpx.AsyncClient
    search: Any
    scrape: Any
    ctr: Any
    usage: _Usage
    errors: list[str]
    memo: dict = field(default_factory=dict)


def _subject(app: App) -> str:
    return f"{app.hint} ({app.name})" if app.kind == "third_party_wrapper" else app.name


def l6_queries(app: App, name: str, attempt: int) -> list[R.Query]:
    bare, paths = R._split_domains(app)
    subject = _subject(app)
    if name == "mcp_official" and paths:  # vendor GitHub org: put it in the text, include_domains is bare
        subject = f"{subject} {' '.join(p.split('/', 1)[1] for p in paths)}"
    domains = list(dict.fromkeys(bare)) or None
    return [R.Query(name, t.format(s=subject), list(domains) if domains else None)
            for t in L6_QUERIES[name][attempt - 1]]


async def _l6_sources(ctx: _Ctx, name: str, attempt: int, seen: set[str]) -> list[R.Source]:
    queries = l6_queries(ctx.app, name, attempt)
    ctx.ctr.queries += len(queries)
    before = len(ctx.ctr.query_errors)
    sem = asyncio.Semaphore(2)
    results = await asyncio.gather(*(R._run_query(ctx.search, q, sem, ctx.ctr) for q in queries))
    if len(ctx.ctr.query_errors) > before:
        ctx.errors.append(f"verify: L6 {name} attempt {attempt}: "
                          + "; ".join(ctx.ctr.query_errors[before:])[:200])
    merged = R.merge_results(ctx.app, list(zip(queries, results)))
    fresh = [s for s in merged if R.normalize_url(s.url) not in seen]
    chosen = R.select_sources(fresh, k=L6_NEW_SOURCES)
    if chosen:
        await R._fetch_all(ctx.scrape, chosen, ctx.ctr)
    for s in chosen:
        if s.snippet and hasattr(ctx.index, "add_snippet"):
            ctx.index.add_snippet(s.url, s.snippet)
    return chosen


async def _context_sources(ctx: _Ctx) -> tuple[list[R.Source], str]:
    """main_blocker: other evaluated fields that verified (supported/partial) as context + quotable sources."""
    lines: list[str] = []
    srcs: list[R.Source] = []
    for f in R.EVALUATED_FIELDS:
        if f == "main_blocker":
            continue
        c = getattr(ctx.record.claims, f)
        st = c.verification.status
        if c.is_unknown() or st not in ("supported", "partially_supported"):
            continue
        lines.append(f"- {f} = {value_json(c.value)} ({st})")
        for ev in c.evidence[:2]:
            text, _ = await _excerpt(ctx.index, f, ev, mark=False)
            if text:
                srcs.append(R.Source(url=ev.url, title=f"evidence page for {f}", tier=ev.source_tier,
                                     groups=["context"], snippet="", best_rank=0, text=text))
    return srcs, "\n".join(lines)


async def _l6_extract(ctx: _Ctx, name: str, sources: list[R.Source], context: str) -> tuple[Optional[Claim], str]:
    for i, s in enumerate(sources, 1):
        s.sid = f"S{i}"
    schema = Extraction.model_fields[name].annotation
    user = build_field_prompt(R.render_app(ctx.app), name, R.render_sources(sources), context)
    try:
        parsed, u = await llm.extract_json(SYSTEM_PROMPT, user, schema)  # type: ignore[arg-type]
    except Exception as e:  # noqa: BLE001
        if is_fatal(e):
            raise
        return None, f"extraction failed ({type(e).__name__})"
    ctx.usage.add_extract(u or {})
    if not isinstance(parsed, schema):  # type: ignore[arg-type]
        parsed = schema.model_validate(parsed)  # type: ignore[union-attr]
    by_id = {s.sid.upper(): s for s in sources}
    errs: list[str] = []
    claim_cls = type(ctx.record.claims).model_fields[name].annotation
    if name in SET_FIELDS:
        value: Any = R._clean_set(list(parsed.value), lower=name == "official_sdks")
    elif name == "main_blocker":
        bt = parsed.type
        value = (Blocker(text=UNKNOWN, type="unknown") if bt == UNKNOWN
                 else Blocker(text=(parsed.text or "").strip() or bt, type=bt))
    else:
        value = parsed.value
    cand = R._claim(name, value, parsed, by_id, errs, _unknown_value(name), claim_cls)
    if cand.is_unknown():
        return None, "extractor returned unknown" + (f" ({errs[0]})" if errs else "")
    return cand, ""


async def _check_candidate(ctx: _Ctx, name: str, cand: Claim, attempt: int) -> tuple[list[LoopResult], bool, str]:
    """Run a re-researched claim through L2, L3(/L4) and a single-claim L5. Returns (entries, grounded, l5)."""
    tag = f"[L6 attempt {attempt} candidate {value_json(cand.value)[:80]}] "
    outcomes = await asyncio.gather(*(loops.verify_evidence(ctx.app, name, ev, ctx.index, ctx.http, memo=ctx.memo)
                                      for ev in cand.evidence))
    entries: list[LoopResult] = []
    for loop, pick, need in (("L2", lambda o: o.l2, False), ("L3", lambda o: o.l3, True),
                             ("L4", lambda o: o.l4, True)):
        agg = loops._aggregate(loop, list(outcomes), pick, need)
        if agg is not None:
            entries.append(agg)
    grounded = _grounded(entries)
    block = await claim_block(ctx.index, name, cand)
    res, _ = await run_l5(ctx.app.name, [(name, block)], ctx.usage, ctx.errors)
    l5 = res.get(name) or _r("L5", "error", "verifier returned no verdict for this claim")
    entries.append(l5)
    return [_r(e.loop, e.result, tag + e.detail) for e in entries], grounded, l5.result


def _replace(ctx: _Ctx, name: str, old: Claim, *, value: Any, evidence: list[Evidence],
             confidence: str, note: str, status: str, vnote: str) -> Claim:
    claim_cls = type(ctx.record.claims).model_fields[name].annotation
    ver = old.verification.model_copy(deep=True)
    ver.status = status  # type: ignore[assignment]
    if vnote:
        ver.notes = "; ".join(x for x in (ver.notes, vnote) if x)
    new = claim_cls(value=value, evidence=evidence, extractor_confidence=confidence, verification=ver, note=note)
    setattr(ctx.record.claims, name, new)
    return new


async def rerun_field(ctx: _Ctx, name: str) -> None:
    """L6 for one evaluated field: up to L6_MAX_ATTEMPTS re-research attempts, then accept or fall back."""
    claim: Claim = getattr(ctx.record.claims, name)
    old_value, old_status, old_unknown = claim.value, claim.verification.status, claim.is_unknown()
    seen = {R.normalize_url(e.url) for e in claim.evidence}
    reasons: list[str] = []
    for attempt in range(1, L6_MAX_ATTEMPTS + 1):
        sources = await _l6_sources(ctx, name, attempt, seen)
        seen |= {R.normalize_url(s.url) for s in sources}
        context = ""
        if name == "main_blocker":
            extra, context = await _context_sources(ctx)
            sources = extra + sources
        if not sources:
            reasons.append(f"a{attempt}: no new sources")
            continue
        cand, why = await _l6_extract(ctx, name, sources, context)
        if cand is None:
            reasons.append(f"a{attempt}: {why}")
            continue
        entries, grounded, l5 = await _check_candidate(ctx, name, cand, attempt)
        claim.verification.loops.extend(entries)
        if not (grounded and l5 == "supported"):
            reasons.append(f"a{attempt}: candidate {value_json(cand.value)[:60]} rejected (L5 {l5}, "
                           f"grounded={'yes' if grounded else 'no'})")
            continue
        urls = ", ".join(sorted({e.url for e in cand.evidence}))[:160]
        if not old_unknown and _same_value(cand.value, old_value):
            seen_ev = {(e.url, e.quote) for e in claim.evidence}
            merged = claim.evidence + [e for e in cand.evidence if (e.url, e.quote) not in seen_ev]
            new = _replace(ctx, name, claim, value=old_value, evidence=merged, confidence=claim.extractor_confidence,
                           note=claim.note, status="supported", vnote=f"L6 confirmed via {urls}")
            new.verification.loops.append(_r("L6", "confirmed", f"{value_json(old_value)} confirmed by new "
                                             f"source(s) {urls} (attempt {attempt}; was {old_status})"))
            return
        why = f"attempt {attempt}: re-researched value supported by verifier and grounded quote; was " \
              f"{'unknown' if old_unknown else old_status}"
        if name in SET_FIELDS and old_status == "partially_supported" and not old_unknown:
            why += "; accepted the re-researched set as a whole (not a union with the old set)"
        new = _replace(ctx, name, claim, value=cand.value, evidence=cand.evidence,
                       confidence=cand.extractor_confidence, note=cand.note, status="supported",
                       vnote=f"L6 changed value ({urls})")
        new.verification.loops.append(_r("L6", "changed", f"{value_json(old_value)} -> {value_json(cand.value)} | {why}"))
        return
    summary = "; ".join(reasons)[:400]
    if old_unknown:
        claim.verification.loops.append(_r("L6", "no_change", f"unknown stays unknown | {summary}"))
    elif old_status == "contradicted":
        unk = _unknown_value(name)
        new = _replace(ctx, name, claim, value=unk, evidence=[], confidence="low",
                       note=f"set unknown by L6: was {value_json(old_value)[:120]}", status="insufficient_evidence",
                       vnote="contradicted by verifier; re-research found no supported value")
        new.verification.loops.append(_r("L6", "set_unknown", f"{value_json(old_value)} -> {value_json(unk)} | "
                                         f"contradicted by verifier; re-research found no supported value ({summary})"))
    else:
        claim.verification.loops.append(_r("L6", "no_change", f"kept {value_json(old_value)[:120]} ({old_status}) "
                                           f"| {summary}"))


async def _safe_rerun(ctx: _Ctx, name: str, sem: Optional[asyncio.Semaphore]) -> None:
    async def go() -> None:
        try:
            await rerun_field(ctx, name)
        except Exception as e:  # noqa: BLE001
            if is_fatal(e):
                raise
            getattr(ctx.record.claims, name).verification.loops.append(
                _r("L6", "error", f"re-research failed: {type(e).__name__}"))
            ctx.errors.append(f"verify: L6 {name} failed ({type(e).__name__})")

    if sem is None:
        await go()
        return
    async with sem:
        await go()


# ------------------------------------------------------------------ main
def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _make_index() -> Any:
    return evidence_mod.EvidenceIndex()


async def verify_app(record: AppRecord, app: App, tools: Any) -> AppRecord:
    """Run L1-L6 over a pass-1 record and return a new pass-2 record (the input is not mutated).

    Raises only on unrecoverable errors (missing/bad verifier or extractor key, unknown model) or when every
    verifier call for the app failed, so the orchestrator can retry or keep the pass-1 copy.
    """
    started = datetime.now(timezone.utc)
    rec = record.model_copy(deep=True)
    rec.pass_ = "pass2"
    search, scrape = tools[0], tools[1]
    usage = _Usage()
    errors: list[str] = []
    ctr = R._Counters()
    index = _make_index()
    names = list(type(rec.claims).model_fields)

    async with _http_client() as http:
        ctx = _Ctx(app=app, record=rec, index=index, http=http, search=search, scrape=scrape, ctr=ctr,
                   usage=usage, errors=errors)
        # L1-L4
        l14 = await loops.run_l1_to_l4(rec, app, index, http)
        for n in names:
            getattr(rec.claims, n).verification.loops.extend(l14.get(n, []))

        # L5: every non-unknown, non-registry claim, batched per app
        items: list[tuple[str, str]] = []
        for n in names:
            c = getattr(rec.claims, n)
            if c.is_unknown() or R.is_registry_claim(c):
                continue
            items.append((n, await claim_block(index, n, c)))
        l5, failed_calls = await run_l5(app.name, items, usage, errors) if items else ({}, 0)
        if items and failed_calls and all(r.result == "error" for r in l5.values()) and \
                all("call failed" in r.detail for r in l5.values()):
            raise RuntimeError(f"verifier failed for all {len(items)} claims of app {app.id}")

        # Pre-L6 status
        for n in names:
            c = getattr(rec.claims, n)
            if R.is_registry_claim(c):
                c.verification.status = "supported"
                c.verification.notes = "registry value (assignment.md), not researched"
                continue
            if c.is_unknown():
                c.verification.status = "insufficient_evidence"
                c.verification.notes = "unknown: no value claimed"
                continue
            res = l5[n]
            c.verification.loops.append(res)
            status, note = pre_l6_status(res.result, _grounded(c.verification.loops),
                                         _has_l1_fail(c.verification.loops))
            c.verification.status = status  # type: ignore[assignment]
            c.verification.notes = note

        # L6: evaluated fields that are not supported (or unknown) get targeted re-research
        triggered = [n for n in R.EVALUATED_FIELDS
                     if getattr(rec.claims, n).is_unknown() or getattr(rec.claims, n).verification.status != "supported"]
        sem = asyncio.Semaphore(L6_FIELD_CONCURRENCY)
        await asyncio.gather(*(_safe_rerun(ctx, n, sem) for n in triggered if n != "main_blocker"))
        if "main_blocker" in triggered:  # derived from the other verified fields: runs after them
            await _safe_rerun(ctx, "main_blocker", None)

        # Non-evaluated fields: no re-research; contradicted values are withdrawn
        for n in names:
            if n in R.EVALUATED_FIELDS:
                continue
            c = getattr(rec.claims, n)
            if c.is_unknown() or c.verification.status != "contradicted":
                continue
            unk = _unknown_value(n)
            new = _replace(ctx, n, c, value=unk, evidence=[], confidence="low",
                           note=f"set unknown: was {value_json(c.value)[:120]}", status="insufficient_evidence",
                           vnote="contradicted by verifier, not re-researched")
            new.verification.loops.append(_r("L6", "set_unknown", f"{value_json(c.value)} -> {value_json(unk)} | "
                                             "contradicted by verifier, not re-researched"))

    s = get_settings()
    rec.record_confidence = R._compute_confidence(rec.claims)  # type: ignore[assignment]
    rec.run = rec.run.model_copy(update={
        "model": s.extractor_model, "verifier_model": s.verifier_model,
        "started_at": started, "finished_at": datetime.now(timezone.utc),
        "tool_calls": ctr.total, "tokens_in": usage.extract_in, "tokens_out": usage.extract_out,
        "cost_usd": R._cost(usage.extract_in, usage.extract_out), "backend": R._backend_name(search),
        "verifier_tokens_in": usage.verifier_in, "verifier_tokens_out": usage.verifier_out,
        "l6_tool_calls": ctr.total, "l6_rerun_claims": len(triggered),
    })
    rec.errors = [*rec.errors, *dict.fromkeys(errors)]
    return AppRecord.model_validate(rec.model_dump(by_alias=True))
