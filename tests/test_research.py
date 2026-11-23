"""Research module tests: no network, no keys."""
import asyncio
import json
import re
from datetime import datetime, timezone

import pytest
from openai.lib._pydantic import to_strict_json_schema

from agent import llm
from agent import research as R
from agent.prompts import Extraction
from agent.registry import App
from agent.schema import AppRecord, RunInfo
from agent.tools.base import PageContent, SearchResult

HUB = App(id=2, name="HubSpot", category="CRM and Sales", website="https://hubspot.com",
          hint="developers.hubspot.com", official_domains=["hubspot.com", "github.com/HubSpot"])
OSS = App(id=58, name="Sherlock", category="Data, SEO and Scraping",
          website="https://github.com/sherlock-project/sherlock", hint="github.com/sherlock-project/sherlock",
          official_domains=["github.com/sherlock-project", "sherlockproject.xyz"], kind="oss_tool")


# ------------------------------------------------------------------ helpers
def _f(value, cites=(), conf="high", note=""):
    return {"citations": [{"source_id": s, "quote": q} for s, q in cites], "confidence": conf,
            "note": note, "value": value}


def _unknown_extraction(**over) -> Extraction:
    d = {
        "description": _f("unknown", conf="low"),
        "auth_methods": _f(["unknown"], conf="low"),
        "credential_access": _f("unknown", conf="low"),
        "production_gate": _f("unknown", conf="low"),
        "api_type": _f(["unknown"], conf="low"),
        "api_breadth": _f("unknown", conf="low"),
        "mcp_official": _f("unknown", conf="low"),
        "mcp_third_party": _f("unknown", conf="low"),
        "free_tier_or_sandbox": _f("unknown", conf="low"),
        "webhooks": _f("unknown", conf="low"),
        "official_sdks": _f(["unknown"], conf="low"),
        "rate_limit_note": _f("unknown", conf="low"),
        "open_source_self_host": _f("unknown", conf="low"),
        "main_blocker": {"citations": [], "confidence": "low", "note": "", "text": "unknown", "type": "unknown"},
        "docs_url": _f("unknown", conf="low"),
    }
    d.update(over)
    return Extraction.model_validate(d)


def _sources():
    s1 = R.Source(url="https://developers.hubspot.com/docs/api/oauth", title="OAuth", tier="official_docs",
                  groups=["auth"], snippet="", best_rank=0, text="HubSpot uses OAuth 2.0 for apps.",
                  fetched_at="2026-11-23T00:00:00+00:00", sid="S1")
    s2 = R.Source(url="https://github.com/someone/hubspot-mcp", title="mcp", tier="community",
                  groups=["mcp_third_party"], snippet="A HubSpot MCP server.", best_rank=0,
                  text="A HubSpot MCP server.", sid="S2")
    return [s1, s2]


def _run():
    return RunInfo(model="m", started_at=datetime.now(timezone.utc), backend="direct")


CAT = ("available", "hubspot", "composio_docs_catalog")


# ------------------------------------------------------------------ planning
def test_plan_queries_groups_and_domains():
    qs = R.plan_queries(HUB)
    assert [q.group for q in qs] == list(R.GROUPS) and len(qs) == 6
    for q in qs:
        assert "HubSpot" in q.text
        if q.group == "mcp_third_party":
            assert q.include_domains is None
        else:
            assert "hubspot.com" in q.include_domains
            assert all("/" not in d for d in q.include_domains)
    # path entry github.com/HubSpot -> org goes in the text, host in domains
    assert "HubSpot" in qs[0].text and "github.com" in qs[0].include_domains


def test_plan_queries_oss_tool():
    qs = R.plan_queries(OSS)
    assert len(qs) == 6
    assert "README" in qs[0].text and "sherlock-project" in qs[0].text
    assert "pricing" not in " ".join(q.text for q in qs)
    assert set(qs[0].include_domains) == {"sherlockproject.xyz", "github.com"}


def test_plan_queries_third_party_wrapper():
    app = App(id=99, name="YouTube Transcript", category="AI, Research and Media-native",
              website="https://transcriptapi.com", hint="transcriptapi.com", official_domains=["transcriptapi.com"],
              kind="third_party_wrapper")
    qs = R.plan_queries(app)
    assert qs[0].text.startswith("transcriptapi.com") and qs[0].include_domains == ["transcriptapi.com"]


# ------------------------------------------------------------------ tiering
@pytest.mark.parametrize("url,tier", [
    ("https://developers.hubspot.com/docs/api/overview", "official_docs"),
    ("https://hubspot.com/docs/x", "official_docs"),
    ("https://knowledge.hubspot.com/integrations/foo", "official_help"),
    ("https://www.hubspot.com/pricing", "official_help"),
    ("https://github.com/HubSpot/hubspot-api-python", "official_github"),
    ("https://github.com/randomdev/hubspot-mcp", "community"),
    ("https://community.hubspot.com/t5/x", "community"),
    ("https://stackoverflow.com/questions/1", "community"),
    ("https://www.reddit.com/r/hubspot", "community"),
    ("https://evilhubspot.com/docs/api", "article"),
    ("https://medium.com/@x/hubspot-api", "article"),
])
def test_source_tier(url, tier):
    assert R.source_tier(HUB, url) == tier


def test_dedupe_by_normalised_url():
    q1, q2 = R.Query("docs", "a"), R.Query("auth", "b")
    res = R.merge_results(HUB, [
        (q1, [SearchResult("https://www.hubspot.com/docs/api/", "A", "short"),
              SearchResult("https://hubspot.com/docs/other", "B", "x")]),
        (q2, [SearchResult("https://hubspot.com/docs/api?utm_source=x#frag", "A", "a much longer snippet")]),
    ])
    assert len(res) == 2
    s = next(r for r in res if "other" not in r.url)
    assert s.groups == ["docs", "auth"] and s.snippet == "a much longer snippet"


def test_select_sources_covers_groups_and_prefers_official():
    srcs = [R.Source(f"https://hubspot.com/docs/{i}", "", "official_docs", ["docs"], "", i) for i in range(10)]
    srcs.append(R.Source("https://github.com/x/hubspot-mcp", "", "community", ["mcp_third_party"], "", 0))
    chosen = R.select_sources(srcs, k=8)
    assert len(chosen) == 8
    assert any(s.tier == "community" for s in chosen)  # coverage beats tier for an uncovered group
    assert chosen[0].tier == "official_docs" and [s.sid for s in chosen] == [f"S{i}" for i in range(1, 9)]


def test_trim_text_keeps_keyword_windows_verbatim():
    filler = "lorem ipsum dolor sit amet " * 800
    text = filler + "Authenticate with OAuth 2.0 using the authorization code flow." + filler
    out = R.trim_text(text, budget=3000)
    assert len(out) <= 3000 + 50
    assert "Authenticate with OAuth 2.0 using the authorization code flow." in out
    for chunk in out.split("\n[...]\n"):
        assert chunk in text


# ------------------------------------------------------------------ to_record
def test_to_record_maps_evidence_and_downgrades():
    x = _unknown_extraction(
        auth_methods=_f(["oauth2", "unknown", "oauth2"], [("S1", "HubSpot uses OAuth 2.0 for apps.")]),
        mcp_third_party=_f("yes", [("S2", "A HubSpot MCP server.")], conf="medium"),
        credential_access=_f("self_serve_free", []),  # no evidence -> downgrade
        webhooks=_f("yes", [("S9", "webhooks!")]),  # invalid id -> dropped -> downgrade
        api_type=_f(["rest"], [("S1", "   ")]),  # empty quote -> downgrade
        description=_f(" ".join(["word"] * 30), [("S1", "HubSpot uses OAuth 2.0 for apps.")]),
        docs_url=_f("developers.hubspot.com", [("S1", "x")]),
    )
    rec = R.to_record(HUB, x, _sources(), CAT, _run())
    c = rec.claims
    assert c.auth_methods.value == ["oauth2"]
    ev = c.auth_methods.evidence[0]
    assert ev.url == "https://developers.hubspot.com/docs/api/oauth" and ev.source_tier == "official_docs"
    assert ev.quote == "HubSpot uses OAuth 2.0 for apps." and ev.fetched_at is not None
    assert c.auth_methods.extractor_confidence == "high"
    assert c.mcp_third_party.value == "yes" and c.mcp_third_party.evidence[0].source_tier == "community"
    assert c.credential_access.value == "unknown" and c.webhooks.value == "unknown"
    assert c.api_type.value == ["unknown"]
    assert c.docs_url.value == "unknown"
    assert len(c.description.value.split()) == 20
    errs = " | ".join(rec.errors)
    assert "credential_access: downgraded to unknown (no evidence)" in errs
    assert "webhooks: dropped citation with invalid source id 'S9'" in errs
    assert "webhooks: downgraded to unknown" in errs
    assert "api_type: downgraded" in errs and "docs_url: downgraded" in errs
    assert "description: truncated" in errs
    # category from registry, composio from catalog
    assert c.category.value == "CRM and Sales" and c.category.note == "registry"
    assert rec.composio_toolkit.value == "available" and rec.composio_toolkit.slug == "hubspot"
    assert rec.composio_toolkit.source == "composio_docs_catalog"
    assert rec.pass_ == "pass1"


def test_to_record_blocker_and_confidence():
    q = ("S1", "HubSpot uses OAuth 2.0 for apps.")
    x = _unknown_extraction(
        auth_methods=_f(["oauth2"], [q]), credential_access=_f("self_serve_free", [q]),
        production_gate=_f("marketplace_listing", [q]), api_type=_f(["rest"], [q]),
        mcp_official=_f("no_found", [q]),
        main_blocker={"citations": [{"source_id": "S1", "quote": q[1]}], "confidence": "medium", "note": "",
                      "text": "Marketplace review", "type": "access"},
    )
    rec = R.to_record(HUB, x, _sources(), CAT, _run())
    assert rec.claims.main_blocker.value.type == "access"
    assert rec.record_confidence == "high"
    assert R.to_record(HUB, _unknown_extraction(), _sources(), CAT, _run()).record_confidence == "low"
    assert R.to_record(HUB, _unknown_extraction(), _sources(), CAT, _run()).errors == []


def test_record_round_trip(tmp_path):
    from agent.schema import dump_record, load_record
    x = _unknown_extraction(auth_methods=_f(["oauth2"], [("S1", "HubSpot uses OAuth 2.0 for apps.")]))
    rec = R.to_record(HUB, x, _sources(), CAT, _run())
    p = tmp_path / "r.json"
    dump_record(rec, p)
    assert json.loads(p.read_text())["pass"] == "pass1"
    back = load_record(p)
    assert back == rec and isinstance(back, AppRecord)


def test_strict_schema_generation():
    s = to_strict_json_schema(Extraction)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(Extraction.model_fields)
    for d in s["$defs"].values():
        assert d["additionalProperties"] is False and set(d["required"]) == set(d["properties"])


# ------------------------------------------------------------------ catalog
class _HasOnly:
    async def has_toolkit(self, name):
        return "unavailable", None


class _Lookup:
    async def has_toolkit(self, name):
        raise AssertionError("lookup should be preferred")

    async def lookup(self, name):
        return "available", "hubspot", "composio_docs_catalog"


def test_catalog_lookup_variants():
    ctr = R._Counters()
    assert asyncio.run(R.catalog_lookup(_Lookup(), HUB, ctr)) == CAT
    assert asyncio.run(R.catalog_lookup(_HasOnly(), HUB, ctr)) == ("unavailable", None, "composio_catalog")
    assert ctr.catalog == 2


# ------------------------------------------------------------------ end-to-end
class FakeSearch:
    def __init__(self):
        self.calls = []

    async def search(self, query, n=8, include_domains=None):
        self.calls.append((query, include_domains))
        if include_domains is None:
            return [SearchResult("https://github.com/randomdev/hubspot-mcp", "hubspot-mcp",
                                 "A HubSpot MCP server.")]
        if "authentication" in query:
            return [SearchResult("https://developers.hubspot.com/docs/api/oauth", "OAuth", "short")]
        if "app review" in query:
            return []  # forces the widened fallback search
        return [SearchResult("https://developers.hubspot.com/docs/api/overview", "Overview",
                             "HubSpot APIs let you build integrations.")]


class FakeScrape:
    async def fetch(self, url):
        if "oauth" in url:
            return PageContent(url, "OAuth", "HubSpot uses OAuth 2.0 for apps. " * 3, 200,
                               "2026-11-23T00:00:00+00:00", "fake")
        if "overview" in url:
            raise RuntimeError("boom")  # falls back to the snippet
        return PageContent(url, "", "", 200, "2026-11-23T00:00:00+00:00", "fake")


def test_research_app_end_to_end(monkeypatch):
    seen = {}

    async def fake_extract(system, user, schema):
        seen["user"] = user
        assert schema is Extraction and "VERBATIM" in system
        oauth = re.search(r"\[(S\d)\] url: https://developers.hubspot.com/docs/api/oauth", user).group(1)
        mcp = re.search(r"\[(S\d)\] url: https://github.com/randomdev", user).group(1)
        return _unknown_extraction(
            auth_methods=_f(["oauth2"], [(oauth, "HubSpot uses OAuth 2.0 for apps.")]),
            mcp_third_party=_f("yes", [(mcp, "A HubSpot MCP server.")]),
        ), {"input_tokens": 1000, "output_tokens": 200}

    monkeypatch.setattr(llm, "extract_json", fake_extract)
    monkeypatch.setenv("EXTRACTOR_PRICE_IN", "1.0")
    monkeypatch.setenv("EXTRACTOR_PRICE_OUT", "10.0")
    search = FakeSearch()
    rec = asyncio.run(R.research_app(HUB, (search, FakeScrape(), _Lookup())))
    assert isinstance(rec, AppRecord)
    assert "[S1] url: https://developers.hubspot.com/docs/api/overview" in seen["user"]  # most groups
    assert "HubSpot APIs let you build integrations." in seen["user"]  # snippet fallback
    assert rec.claims.auth_methods.value == ["oauth2"]
    assert rec.claims.mcp_third_party.evidence[0].source_tier == "community"
    assert rec.composio_toolkit.slug == "hubspot"
    # 6 queries + 1 widened retry, 3 fetches, 1 catalog
    assert len(search.calls) == 7 and rec.run.tool_calls == 7 + 3 + 1
    assert rec.run.tokens_in == 1000 and rec.run.tokens_out == 200
    assert rec.run.cost_usd == pytest.approx(0.001 + 0.002)
    assert rec.run.finished_at is not None and rec.run.backend == "FakeSearch"


def test_research_app_retries_extraction_once_then_raises(monkeypatch):
    calls = []

    async def bad(system, user, schema):
        calls.append(1)
        raise ValueError("invalid")

    monkeypatch.setattr(llm, "extract_json", bad)
    with pytest.raises(RuntimeError, match="extraction failed twice"):
        asyncio.run(R.research_app(HUB, (FakeSearch(), FakeScrape(), _HasOnly())))
    assert len(calls) == 2


def test_research_app_retry_succeeds(monkeypatch):
    calls = []

    async def flaky(system, user, schema):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("invalid")
        return _unknown_extraction(), {"input_tokens": 5, "output_tokens": 1}

    monkeypatch.delenv("EXTRACTOR_PRICE_IN", raising=False)
    monkeypatch.setattr(llm, "extract_json", flaky)
    rec = asyncio.run(R.research_app(HUB, (FakeSearch(), FakeScrape(), _HasOnly())))
    assert rec.run.cost_usd == 0.0 and rec.composio_toolkit.value == "unavailable"


def test_select_sources_third_party_mcp_needs_non_official():
    srcs = [R.Source(f"https://hubspot.com/docs/{i}", "", "official_docs", ["docs", "mcp_third_party"], "", i)
            for i in range(10)]
    srcs.append(R.Source("https://github.com/x/hubspot-mcp", "", "community", ["mcp_third_party"], "", 3))
    chosen = R.select_sources(srcs, k=8)
    assert any(s.url == "https://github.com/x/hubspot-mcp" for s in chosen)


# ------------------------------------------------------------------ review fixes
def _src(url, tier, groups, rank, group_rank=None):
    return R.Source(url, "", tier, list(groups), "", rank, group_rank=group_rank or {g: rank for g in groups})


def test_select_sources_distinct_slots_per_group_and_access_quota():
    # Docs pages that were ALSO returned by the access/gate queries must not consume those groups' slots.
    srcs = [_src(f"https://hubspot.com/docs/{i}", "official_docs", ["docs", "access", "gate"], i,
                 {"docs": i, "access": i + 2, "gate": i + 1}) for i in range(12)]
    srcs += [_src("https://hubspot.com/pricing", "official_help", ["access"], 0),
             _src("https://hubspot.com/plans", "official_help", ["access"], 1),
             _src("https://hubspot.com/partners", "official_help", ["gate"], 0),
             _src("https://github.com/x/hubspot-mcp", "community", ["mcp_third_party"], 0)]
    chosen = {s.url for s in R.select_sources(srcs, k=R.MAX_SOURCES)}
    assert len(chosen) == R.MAX_SOURCES == 10
    assert {"https://hubspot.com/pricing", "https://hubspot.com/plans", "https://hubspot.com/partners",
            "https://github.com/x/hubspot-mcp"} <= chosen


def test_select_sources_uses_rank_within_group():
    a = _src("https://hubspot.com/docs/a", "official_docs", ["docs", "auth"], 0, {"docs": 0, "auth": 4})
    b = _src("https://hubspot.com/docs/b", "official_docs", ["auth"], 1, {"auth": 0})
    chosen = R.select_sources([a, b], k=2)
    assert {s.url for s in chosen} == {a.url, b.url}
    merged = R.merge_results(HUB, [(R.Query("docs", "q"), [SearchResult(a.url, "", "")]),
                                   (R.Query("auth", "q"), [SearchResult(b.url, "", ""), SearchResult(a.url, "", "")])])
    assert next(s for s in merged if s.url == a.url).group_rank == {"docs": 0, "auth": 1}


def test_registry_category_is_marked():
    rec = R.to_record(HUB, _unknown_extraction(), _sources(), CAT, _run())
    assert R.is_registry_claim(rec.claims.category)
    assert rec.claims.category.evidence[0].quote.startswith(R.REGISTRY_QUOTE_PREFIX)
    assert not R.is_registry_claim(rec.claims.auth_methods)


class _FailingSearch:
    def __init__(self, fail_groups=None):
        self.fail_all = fail_groups is None

    async def search(self, query, n=8, include_domains=None):
        if self.fail_all or "authentication" in query:
            raise RuntimeError("429 Too Many Requests")
        return [SearchResult("https://developers.hubspot.com/docs/api/overview", "Overview", "HubSpot APIs.")]


def test_partial_search_failure_is_recorded(monkeypatch):
    async def ok(system, user, schema):
        return _unknown_extraction(), {"input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(llm, "extract_json", ok)
    rec = asyncio.run(R.research_app(HUB, (_FailingSearch(fail_groups=["auth"]), FakeScrape(), _HasOnly())))
    assert "search[auth] failed: RuntimeError" in rec.errors


def test_all_searches_failing_raises_instead_of_fake_unknown_record(monkeypatch):
    async def never(system, user, schema):
        raise AssertionError("extractor must not run when every search failed")

    monkeypatch.setattr(llm, "extract_json", never)
    with pytest.raises(RuntimeError, match="all 6 searches failed"):
        asyncio.run(R.research_app(HUB, (_FailingSearch(), FakeScrape(), _HasOnly())))


def test_catalog_lookup_uses_tool_source_attribute():
    from agent.tools.composio_tools import ComposioCatalog, DocsCatalog
    assert DocsCatalog.source == "composio_docs_catalog" and ComposioCatalog.source == "composio_catalog"

    class Docs(_HasOnly):
        source = "composio_docs_catalog"

    assert asyncio.run(R.catalog_lookup(Docs(), HUB, R._Counters()))[2] == "composio_docs_catalog"
