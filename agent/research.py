"""Research agent, pass 1 (PRD FR-02, FR-03, FR-05).

Pipeline per app:
    plan_queries -> run searches (bounded) -> dedupe + tier -> select ~8 sources
    -> fetch (richer of fetched text vs. search snippet) -> keyword-window trim
    -> one structured-output extraction call -> to_record (structural integrity only)

`to_record` is NOT verification (that is Phase 3). It only enforces schema integrity:
a non-unknown value without a valid citation is downgraded to unknown and the reason is
appended to `record.errors`.

Public interface used by the orchestrator:
    async def research_app(app, tools, *, pass_="pass1") -> AppRecord
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from agent import llm  # module import so tests can monkeypatch llm.extract_json
from agent.config import get_settings
from agent.prompts import SYSTEM_PROMPT, Extraction, build_user_prompt
from agent.registry import App, is_official_url
from agent.schema import (
    UNKNOWN,
    AppRecord,
    Blocker,
    Claim,
    Claims,
    ComposioToolkit,
    Evidence,
    RunInfo,
)
from agent.tools.base import CatalogTool, PageContent, ScrapeTool, SearchResult, SearchTool

# ------------------------------------------------------------------ constants
GROUPS: tuple[str, ...] = ("docs", "auth", "access", "gate", "mcp_official", "mcp_third_party")
RESULTS_PER_QUERY = 5
SEARCH_CONCURRENCY = 4
MAX_SOURCES = 10
# Guaranteed distinct source slots per query group (when that group returned anything). The rest of
# MAX_SOURCES is filled by global rank. Access/pricing gets 2: plan pages and developer-account pages
# are often separate, and credential_access is the field most sensitive to a missing page.
GROUP_QUOTA: dict[str, int] = {"access": 2}
PER_SOURCE_CHARS = 6000
TOTAL_CHARS = 45000
MAX_QUOTE_CHARS = 300  # hard cap applied in code; the prompt asks for <= PROMPT_QUOTE_CHARS
PROMPT_QUOTE_CHARS = 200
MAX_DESCRIPTION_WORDS = 20

TIER_RANK = {"official_docs": 0, "official_help": 1, "official_github": 2, "article": 3, "community": 4}
OFFICIAL_TIERS = {"official_docs", "official_help", "official_github"}
EVALUATED_FIELDS = ("auth_methods", "credential_access", "production_gate", "api_type",
                    "mcp_official", "main_blocker")

KEYWORDS: tuple[str, ...] = (
    "oauth", "api key", "token", "authentication", "authorization", "pricing", "plan", "free",
    "trial", "partner", "app review", "marketplace", "mcp", "model context protocol", "rate limit",
    "webhook", "sdk", "graphql", "rest", "endpoint",
)

_DOCS_HOST_PREFIXES = ("docs.", "developer.", "developers.", "dev.", "api.", "apidocs.", "reference.")
_DOCS_PATH_RE = re.compile(r"/(docs?|api|apis|developers?|reference|documentation|dev|guides?)(/|$)", re.I)
_HELP_HOST_PREFIXES = ("help.", "support.", "kb.", "knowledge.")
_HELP_PATH_RE = re.compile(r"/(help|support|pricing|plans?|kb|knowledge-?base|faq|hc)(/|$)", re.I)
_COMMUNITY_HOSTS = (
    "github.com", "gitlab.com", "reddit.com", "stackoverflow.com", "stackexchange.com",
    "news.ycombinator.com", "discord.com", "discourse.org", "mcp.so", "smithery.ai", "glama.ai",
    "pulsemcp.com", "mcpservers.org", "npmjs.com", "pypi.org", "huggingface.co",
)
_TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_|source$|gclid$|fbclid$)", re.I)

# The category claim is NOT researched: it is copied from the registry (assignment.md) with a labelled
# pseudo-quote. Quote grounding (L3) and accuracy scoring must skip it: use is_registry_claim().
REGISTRY_NOTE = "registry"
REGISTRY_QUOTE_PREFIX = "Registry category (assignment.md): "


def is_registry_evidence(ev: Any) -> bool:
    return str(getattr(ev, "quote", "")).startswith(REGISTRY_QUOTE_PREFIX)


def is_registry_claim(claim: Any) -> bool:
    """True for a claim filled from the registry rather than from fetched sources."""
    return getattr(claim, "note", "") == REGISTRY_NOTE and all(
        is_registry_evidence(e) for e in getattr(claim, "evidence", []))


# ------------------------------------------------------------------- models
@dataclass
class Query:
    group: str
    text: str
    include_domains: Optional[list[str]] = None


@dataclass
class Source:
    url: str
    title: str
    tier: str
    groups: list[str]
    snippet: str
    best_rank: int  # best position within any result list (0 = top)
    text: str = ""
    group_rank: dict[str, int] = field(default_factory=dict)  # group -> position in that group's results
    fetched_at: Optional[str] = None
    sid: str = ""  # "S1".. assigned at selection


@dataclass
class _Counters:
    searches: int = 0
    fetches: int = 0
    catalog: int = 0
    queries: int = 0  # logical queries planned
    query_errors: list[str] = field(default_factory=list)  # one entry per query whose search raised

    @property
    def total(self) -> int:
        return self.searches + self.fetches + self.catalog


# ----------------------------------------------------------------- planning
def _split_domains(app: App) -> tuple[list[str], list[str]]:
    """(bare domains, github-style 'host/org' path entries)."""
    bare = [d.strip().rstrip("/") for d in app.official_domains if "/" not in d.strip().rstrip("/")]
    paths = [d.strip().rstrip("/") for d in app.official_domains if "/" in d.strip().rstrip("/")]
    return bare, paths


def plan_queries(app: App) -> list[Query]:
    """Six FR-02 query groups, seeded with name + hint; official groups restricted to official domains."""
    bare, paths = _split_domains(app)
    orgs = " ".join(p.split("/", 1)[1] for p in paths)
    # Path entries (github.com/org) cannot be expressed as include_domains: widen to the host,
    # add the org to the query text, and let tiering decide what is official.
    official = list(dict.fromkeys(bare + [p.split("/", 1)[0] for p in paths])) or None
    subject = app.name
    if app.kind == "third_party_wrapper":
        subject = f"{app.hint} ({app.name})"  # research the hinted provider, not the wrapped product
    seed = f"{subject} {app.hint}" if app.kind != "third_party_wrapper" else subject
    seed = f"{seed} {orgs}".strip()

    if app.kind in ("oss_tool", "cli"):
        texts = {
            "docs": f"{seed} README usage documentation",
            "auth": f"{seed} installation command line options configuration",
            "access": f"{seed} license open source install pip npm",
            "gate": f"{seed} programmatic usage library API python",
        }
    else:
        texts = {
            "docs": f"{seed} API documentation reference endpoints",
            "auth": f"{seed} API authentication OAuth API key access token",
            "access": f"{seed} API access pricing plans free tier trial developer account",
            "gate": f"{seed} app review marketplace listing partner program publish integration",
        }
    texts["mcp_official"] = f"{seed} official MCP server Model Context Protocol"
    texts["mcp_third_party"] = f"{app.name} MCP server Model Context Protocol github"
    return [
        Query(g, texts[g], None if g == "mcp_third_party" else (list(official) if official else None))
        for g in GROUPS
    ]


# ---------------------------------------------------------------- tiering
def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    path = re.sub(r"/+$", "", p.path) or ""
    q = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not _TRACKING_PARAMS.match(k)])
    return urlunparse(("https", host, path, "", q, ""))


def _host_matches(host: str, dom: str) -> bool:
    return host == dom or host.endswith("." + dom)


def source_tier(app: App, url: str) -> str:
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path or "/"
    # GitHub org/path entries are case-insensitive (github.com/HubSpot == github.com/hubspot).
    if is_official_url(app, url.lower()) or is_official_url(app, url):
        if host == "github.com":
            return "official_github"
        if host.startswith("community.") or host.startswith("forum"):
            return "community"  # vendor-hosted user forum: user content, not documentation
        if host.startswith(_DOCS_HOST_PREFIXES) or _DOCS_PATH_RE.search(path) or host.endswith(".github.io"):
            return "official_docs"
        return "official_help"  # help/support/pricing and other official pages
    if any(_host_matches(host, h) for h in _COMMUNITY_HOSTS) or "forum" in host or host.startswith("community."):
        return "community"
    return "article"


# ------------------------------------------------------------------ search
async def _run_query(search: SearchTool, q: Query, sem: asyncio.Semaphore,
                     ctr: _Counters) -> list[SearchResult]:
    async with sem:
        err: Optional[BaseException] = None
        ctr.searches += 1
        try:
            res = await search.search(q.text, n=RESULTS_PER_QUERY, include_domains=q.include_domains)
        except Exception as e:  # noqa: BLE001 - one failed query must not sink the app
            res, err = [], e
        if not res and q.include_domains:  # official domains not indexed (or failed): widen once
            ctr.searches += 1
            try:
                res = await search.search(q.text, n=RESULTS_PER_QUERY, include_domains=None)
                err = None
            except Exception as e:  # noqa: BLE001
                res, err = [], e
        if err is not None:  # still failed after tool-level retries: surface it, never hide it
            ctr.query_errors.append(f"search[{q.group}] failed: {type(err).__name__}")
        return res


def merge_results(app: App, per_query: list[tuple[Query, list[SearchResult]]]) -> list[Source]:
    """Dedupe by normalised URL; keep the longest snippet and every group a URL answered."""
    by_url: dict[str, Source] = {}
    for q, results in per_query:
        for rank, r in enumerate(results):
            if not r.url or not r.url.lower().startswith(("http://", "https://")):
                continue
            key = normalize_url(r.url)
            s = by_url.get(key)
            if s is None:
                by_url[key] = Source(url=r.url, title=r.title or "", tier=source_tier(app, r.url),
                                     groups=[q.group], snippet=r.snippet or "", best_rank=rank,
                                     group_rank={q.group: rank})
                continue
            if q.group not in s.groups:
                s.groups.append(q.group)
            s.group_rank[q.group] = min(s.group_rank.get(q.group, rank), rank)
            if len(r.snippet or "") > len(s.snippet):
                s.snippet = r.snippet or ""
            s.best_rank = min(s.best_rank, rank)
    return list(by_url.values())


def select_sources(sources: list[Source], k: int = MAX_SOURCES) -> list[Source]:
    """Tier first, then coverage.

    Each query group gets GROUP_QUOTA[g] (default 1) *distinct* slots, filled with the best sources that
    group's query returned (official first, then position in that group's results). A source already
    chosen for another group does not count toward this group's quota: a docs page that also appeared
    in the pricing results would otherwise crowd the pricing page out. Remaining slots go by global rank.
    """
    def key(s: Source) -> tuple:
        return (TIER_RANK[s.tier], s.best_rank, -len(s.groups))

    def group_key(g: str):
        # Official before third-party, then that group's own result order. Not docs-vs-help tier: a
        # pricing page (official_help) is the *right* answer to the access query, and ranking by tier
        # would let any docs page the access query also returned take the slot.
        return lambda s: (s.tier not in OFFICIAL_TIERS, s.group_rank.get(g, s.best_rank),
                          TIER_RANK[s.tier], -len(s.groups))

    ranked = sorted(sources, key=key)
    pools: dict[str, list[Source]] = {}
    for g in GROUPS:
        members = [s for s in sources if g in s.groups]
        if g == "mcp_third_party":
            # Third-party MCP evidence must come from a non-official source: an official page returned
            # by that query (e.g. the vendor's own MCP doc) does not cover the group.
            members = sorted((s for s in members if s.tier not in OFFICIAL_TIERS),
                             key=lambda s: (s.group_rank.get(g, s.best_rank), TIER_RANK[s.tier]))
        else:
            members.sort(key=group_key(g))
        pools[g] = members

    chosen: list[Source] = []
    taken: set[int] = set()
    rounds = max([1, *GROUP_QUOTA.values()])
    for rnd in range(rounds):  # round-robin so every group gets its first slot before any second slot
        for g in GROUPS:
            if len(chosen) >= k:
                break
            if rnd >= GROUP_QUOTA.get(g, 1):
                continue
            for s in pools[g]:
                if id(s) not in taken:
                    chosen.append(s)
                    taken.add(id(s))
                    break
    for s in ranked:
        if len(chosen) >= k:
            break
        if id(s) not in taken:
            chosen.append(s)
            taken.add(id(s))
    chosen.sort(key=key)
    for i, s in enumerate(chosen, 1):
        s.sid = f"S{i}"
    return chosen


async def _fetch_all(scrape: ScrapeTool, sources: list[Source], ctr: _Counters) -> None:
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

    async def one(s: Source) -> None:
        page: Optional[PageContent] = None
        async with sem:
            ctr.fetches += 1
            try:
                page = await scrape.fetch(s.url)
            except Exception:  # noqa: BLE001 - fall back to the search snippet
                page = None
        fetched = page.text if page is not None and page.status < 400 else ""
        if page is not None and page.title and not s.title:
            s.title = page.title
        # Many docs pages are JS shells: keep whichever text is richer.
        if len(fetched or "") >= len(s.snippet):
            s.text = fetched or ""
            s.fetched_at = page.fetched_at if page is not None else None
        else:
            s.text = s.snippet
        if not s.fetched_at:
            s.fetched_at = datetime.now(timezone.utc).isoformat()

    await asyncio.gather(*(one(s) for s in sources))


# ---------------------------------------------------------------- trimming
def trim_text(text: str, budget: int = PER_SOURCE_CHARS, window: int = 450) -> str:
    """Keep the page intro plus keyword windows (verbatim substrings) up to `budget` chars."""
    text = text or ""
    if len(text) <= budget:
        return text
    low = text.lower()
    intro = min(800, budget // 4)
    hits: dict[str, list[int]] = {}
    for kw in KEYWORDS:
        pos, found = 0, []
        while len(found) < 4:
            i = low.find(kw, pos)
            if i < 0:
                break
            found.append(i)
            pos = i + len(kw) + window  # spread hits out
        if found:
            hits[kw] = found
    intervals: list[tuple[int, int]] = [(0, intro)]
    used = intro
    # Round-robin across keywords so one frequent term cannot eat the whole budget.
    depth = 0
    while used < budget and any(len(v) > depth for v in hits.values()):
        for kw, pos_list in hits.items():
            if depth >= len(pos_list) or used >= budget:
                continue
            c = pos_list[depth]
            a, b = max(0, c - window), min(len(text), c + window)
            intervals.append((a, b))
            used += b - a
        depth += 1
    intervals.sort()
    merged: list[list[int]] = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, total = [], 0
    for a, b in merged:
        # snap to whitespace so quotes are not cut mid-word
        if a > 0:
            sp = text.find(" ", a, min(b, a + 40))
            a = sp + 1 if sp >= 0 else a
        if b < len(text):
            sp = text.rfind(" ", max(a, b - 40), b)
            b = sp if sp > a else b
        chunk = text[a:b].strip()
        if not chunk:
            continue
        if total + len(chunk) > budget:
            chunk = chunk[: max(0, budget - total)]
        if not chunk:
            break
        out.append(chunk)
        total += len(chunk)
        if total >= budget:
            break
    return "\n[...]\n".join(out)


def render_sources(sources: list[Source]) -> str:
    per = min(PER_SOURCE_CHARS, TOTAL_CHARS // max(1, len(sources)))
    blocks = []
    for s in sources:
        body = trim_text(s.text or s.snippet, per) or "(no text retrieved)"
        blocks.append(f"[{s.sid}] url: {s.url}\ntier: {s.tier} | title: {s.title.strip()[:150]}\n"
                      f"---\n{body}\n--- end {s.sid} ---")
    return "\n\n".join(blocks)


def render_app(app: App) -> str:
    lines = [f"name: {app.name}", f"website: {app.website}", f"hint: {app.hint}",
             f"kind: {app.kind}", f"official_domains: {', '.join(app.official_domains)}"]
    if app.notes:
        lines.append(f"registry notes (context only, NOT evidence): {app.notes}")
    return "\n".join(lines)


# --------------------------------------------------------------- conversion
def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _evidence(field_name: str, cited: Any, by_id: dict[str, Source], errors: list[str]) -> list[Evidence]:
    out: list[Evidence] = []
    seen: set[tuple[str, str]] = set()
    for c in cited.citations:
        sid = (c.source_id or "").strip().strip("[]").upper()
        src = by_id.get(sid)
        if src is None:
            errors.append(f"{field_name}: dropped citation with invalid source id {c.source_id!r}")
            continue
        quote = (c.quote or "").strip()
        if not quote:
            errors.append(f"{field_name}: dropped citation of {sid} with empty quote")
            continue
        if len(quote) > MAX_QUOTE_CHARS:
            quote = quote[:MAX_QUOTE_CHARS].rstrip()
        if (src.url, quote) in seen:
            continue
        seen.add((src.url, quote))
        out.append(Evidence(url=src.url, quote=quote, source_tier=src.tier,  # type: ignore[arg-type]
                            fetched_at=_parse_dt(src.fetched_at)))
    return out


def _clean_set(values: list[str], lower: bool = False) -> list[str]:
    vals = [v.strip().lower() if lower else v for v in values if isinstance(v, str) and v.strip()]
    vals = list(dict.fromkeys(vals))
    if len(vals) > 1 and UNKNOWN in vals:
        vals = [v for v in vals if v != UNKNOWN]  # enum mixing rule: drop "unknown" when others present
    return vals or [UNKNOWN]


def _claim(field_name: str, value: Any, cited: Any, by_id: dict[str, Source], errors: list[str],
           unknown_value: Any, claim_cls: type = Claim) -> Claim:
    ev = _evidence(field_name, cited, by_id, errors)
    note = cited.note or ""
    is_unknown = (value == unknown_value) or (isinstance(value, Blocker) and value.type == UNKNOWN)
    if is_unknown:
        return claim_cls(value=unknown_value, evidence=ev, extractor_confidence="low", note=note)
    if not ev:
        errors.append(f"{field_name}: downgraded to unknown (no evidence)")
        return claim_cls(value=unknown_value, extractor_confidence="low",
                         note=f"downgraded: no valid evidence. {note}".strip())
    return claim_cls(value=value, evidence=ev, extractor_confidence=cited.confidence, note=note)


def _compute_confidence(claims: Claims) -> str:
    good = 0
    for f in EVALUATED_FIELDS:
        c: Claim = getattr(claims, f)
        if not c.is_unknown() and any(e.source_tier in OFFICIAL_TIERS for e in c.evidence):
            good += 1
    frac = good / len(EVALUATED_FIELDS)
    return "high" if frac >= 0.7 else "low" if frac < 0.4 else "medium"


def to_record(app: App, extraction: Extraction, sources: list[Source],
              catalog_result: tuple[str, Optional[str], str], run: RunInfo, *,
              pass_: str = "pass1") -> AppRecord:
    errors: list[str] = []
    by_id = {s.sid.upper(): s for s in sources}
    F = Claims.model_fields
    x = extraction

    def C(name: str) -> type:
        return F[name].annotation  # the parametrised Claim[...] class for this field

    # category comes from the registry (assignment.md defines it), not from research. The quote is a
    # labelled registry pseudo-quote, not page text: excluded from accuracy and quote grounding.
    category = C("category")(
        value=app.category,
        evidence=[Evidence(url=app.website, quote=f"{REGISTRY_QUOTE_PREFIX}{app.category}",
                           source_tier="official_help")],
        extractor_confidence="high", note=REGISTRY_NOTE,
    )

    desc_val = (x.description.value or "").strip() or UNKNOWN
    desc_claim = _claim("description", desc_val, x.description, by_id, errors, UNKNOWN, C("description"))
    if not desc_claim.is_unknown():
        words = desc_claim.value.split()
        if len(words) > MAX_DESCRIPTION_WORDS:
            desc_claim.value = " ".join(words[:MAX_DESCRIPTION_WORDS])
            desc_claim.note = (desc_claim.note + " truncated to 20 words").strip()
            errors.append("description: truncated to 20 words")

    docs_val = (x.docs_url.value or "").strip() or UNKNOWN
    if docs_val != UNKNOWN and not docs_val.lower().startswith(("http://", "https://")):
        errors.append(f"docs_url: downgraded to unknown (not an http(s) URL: {docs_val[:80]!r})")
        docs_val = UNKNOWN

    rl_val = (x.rate_limit_note.value or "").strip() or UNKNOWN
    blocker_type = x.main_blocker.type
    blocker = Blocker(text=(x.main_blocker.text or "").strip() or blocker_type, type=blocker_type)
    if blocker_type == UNKNOWN:
        blocker = Blocker(text=UNKNOWN, type="unknown")

    def set_claim(name: str, values: list[str], lower: bool = False) -> Claim:
        return _claim(name, _clean_set(values, lower), getattr(x, name), by_id, errors, [UNKNOWN], C(name))

    def enum_claim(name: str) -> Claim:
        return _claim(name, getattr(x, name).value, getattr(x, name), by_id, errors, UNKNOWN, C(name))

    blocker_claim = _claim("main_blocker", blocker, x.main_blocker, by_id, errors,
                           Blocker(text=UNKNOWN, type="unknown"), C("main_blocker"))

    claims = Claims(
        category=category,
        description=desc_claim,
        auth_methods=set_claim("auth_methods", list(x.auth_methods.value)),
        credential_access=enum_claim("credential_access"),
        production_gate=enum_claim("production_gate"),
        api_type=set_claim("api_type", list(x.api_type.value)),
        api_breadth=enum_claim("api_breadth"),
        mcp_official=enum_claim("mcp_official"),
        mcp_third_party=enum_claim("mcp_third_party"),
        free_tier_or_sandbox=enum_claim("free_tier_or_sandbox"),
        webhooks=enum_claim("webhooks"),
        official_sdks=set_claim("official_sdks", list(x.official_sdks.value), lower=True),
        rate_limit_note=_claim("rate_limit_note", rl_val, x.rate_limit_note, by_id, errors, UNKNOWN,
                               C("rate_limit_note")),
        open_source_self_host=enum_claim("open_source_self_host"),
        main_blocker=blocker_claim,
        docs_url=_claim("docs_url", docs_val, x.docs_url, by_id, errors, UNKNOWN, C("docs_url")),
    )
    status, slug, csource = catalog_result
    toolkit = ComposioToolkit(value=status, slug=slug if status == "available" else None,  # type: ignore[arg-type]
                              source=csource)  # type: ignore[arg-type]
    return AppRecord(
        app_id=app.id, app=app.name, pass_=pass_, claims=claims, composio_toolkit=toolkit,
        record_confidence=_compute_confidence(claims),  # type: ignore[arg-type]
        run=run, errors=errors,
    )


# ------------------------------------------------------------------ catalog
async def catalog_lookup(catalog: CatalogTool, app: App, ctr: _Counters) -> tuple[str, Optional[str], str]:
    """Composio toolkit status straight from the catalog (AC-6), never from the LLM."""
    ctr.catalog += 1
    try:
        if hasattr(catalog, "lookup"):
            status, slug, source = await catalog.lookup(app.name)  # type: ignore[attr-defined]
        else:
            status, slug = await catalog.has_toolkit(app.name)
            source = getattr(catalog, "source", "composio_catalog")
    except Exception:  # noqa: BLE001 - coverage unknown is a legal answer
        return "unknown", None, "composio_catalog"
    if source not in ("composio_catalog", "composio_docs_catalog"):
        source = "composio_catalog"
    return status, slug, source


# --------------------------------------------------------------- run info
def _backend_name(search: Any) -> str:
    mod = type(search).__module__
    if "direct" in mod:
        return "direct"
    if "composio" in mod:
        return "composio"
    return type(search).__name__


def _cost(tokens_in: int, tokens_out: int) -> float:
    try:
        pin = float(os.environ.get("EXTRACTOR_PRICE_IN", "") or 0)
        pout = float(os.environ.get("EXTRACTOR_PRICE_OUT", "") or 0)
    except ValueError:
        return 0.0
    return round(tokens_in / 1e6 * pin + tokens_out / 1e6 * pout, 6)


async def _extract(user: str) -> tuple[Extraction, dict[str, int]]:
    """One extraction call, retried once on failure (llm.extract_json already retries transients)."""
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    last: Optional[BaseException] = None
    for _ in range(2):
        try:
            parsed, usage = await llm.extract_json(SYSTEM_PROMPT, user, Extraction)
            for k in usage_total:
                usage_total[k] += int(usage.get(k, 0) or 0)
            if not isinstance(parsed, Extraction):
                parsed = Extraction.model_validate(parsed)
            return parsed, usage_total
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"extraction failed twice: {type(last).__name__}: {last}") from last


# --------------------------------------------------------------------- main
async def gather_sources(app: App, search: SearchTool, scrape: ScrapeTool,
                         ctr: _Counters) -> list[Source]:
    queries = plan_queries(app)
    ctr.queries += len(queries)
    sem = asyncio.Semaphore(SEARCH_CONCURRENCY)
    results = await asyncio.gather(*(_run_query(search, q, sem, ctr) for q in queries))
    merged = merge_results(app, list(zip(queries, results)))
    chosen = select_sources(merged)
    await _fetch_all(scrape, chosen, ctr)
    return chosen


async def research_app(app: App, tools: tuple[SearchTool, ScrapeTool, CatalogTool], *,
                       pass_: str = "pass1") -> AppRecord:
    search, scrape, catalog = tools
    started = datetime.now(timezone.utc)
    ctr = _Counters()
    settings = get_settings()

    catalog_task = asyncio.create_task(catalog_lookup(catalog, app, ctr))
    try:
        sources = await gather_sources(app, search, scrape, ctr)
    except BaseException:
        catalog_task.cancel()
        raise
    catalog_result = await catalog_task

    if ctr.queries and len(ctr.query_errors) >= ctr.queries:
        # Every search raised (bad key, quota, outage): an all-unknown record would look like an
        # honest "nothing found" and be skipped on resume. Fail the app so the orchestrator retries it.
        raise RuntimeError(f"all {ctr.queries} searches failed: {ctr.query_errors[0]}")
    errors_pre: list[str] = list(ctr.query_errors)
    if not sources:
        errors_pre.append("no sources found by search")
    user = build_user_prompt(render_app(app), render_sources(sources) if sources else "(no sources found)")
    extraction, usage = await _extract(user)

    tin, tout = usage["input_tokens"], usage["output_tokens"]
    run = RunInfo(
        model=settings.extractor_model, started_at=started, finished_at=datetime.now(timezone.utc),
        tool_calls=ctr.total, tokens_in=tin, tokens_out=tout, cost_usd=_cost(tin, tout),
        backend=_backend_name(search),
    )
    record = to_record(app, extraction, sources, catalog_result, run, pass_=pass_)
    record.errors = errors_pre + record.errors
    return record
