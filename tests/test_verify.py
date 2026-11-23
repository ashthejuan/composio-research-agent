"""verify.py: L5 verifier + L6 re-research acceptance branches (no network)."""
import asyncio
import json
import re
from datetime import datetime, timezone

import pytest

from agent import llm, loops
from agent import verify as V
from agent.prompts import Citation, Extraction
from agent.registry import App
from agent.research import REGISTRY_NOTE, REGISTRY_QUOTE_PREFIX
from agent.schema import AppRecord, Blocker, Claim, Claims, Evidence, RunInfo
from agent.tools.base import PageContent, SearchResult

APP = App(id=1, name="Acme", category="CRM and Sales", website="https://acme.com", hint="crm",
          official_domains=["acme.com"])
OLD = "https://docs.acme.com/api"
NEW1, NEW2 = "https://docs.acme.com/new1", "https://docs.acme.com/new2"

PAGES = {
    OLD: ("Intro. OAuth 2.0 authorization code flow is supported. Free developer accounts get API keys. "
          "Any app can connect without review. REST API over HTTPS. No MCP server is offered. "
          "Self-serve keys for everyone. Webhooks are supported."),
    NEW1: "Authentication: use OAuth 2.0 or an API key header. Pricing: API access on the free plan for all.",
    NEW2: "Second page. Personal access tokens are sent as Bearer tokens. Partner program required to list apps.",
}

L5_VOCAB = {"supported", "partially_supported", "contradicted", "insufficient_evidence", "error"}
L6_VOCAB = {"changed", "confirmed", "set_unknown", "no_change", "error"}
L14_VOCAB = {"pass", "fail", "skip", "error"}


def ev(quote, url=OLD):
    return Evidence(url=url, quote=quote, source_tier="official_docs")


def base_record() -> AppRecord:
    claims = Claims.all_unknown()
    claims.category = Claim(value="CRM and Sales", note=REGISTRY_NOTE, extractor_confidence="high",
                            evidence=[ev(REGISTRY_QUOTE_PREFIX + "CRM and Sales", "https://acme.com/")])
    claims.auth_methods = Claim(value=["oauth2"], evidence=[ev("OAuth 2.0 authorization code flow is supported")])
    claims.credential_access = Claim(value="self_serve_free", evidence=[ev("Free developer accounts get API keys")])
    claims.production_gate = Claim(value="none", evidence=[ev("Any app can connect without review")])
    claims.api_type = Claim(value=["rest"], evidence=[ev("REST API over HTTPS")])
    claims.mcp_official = Claim(value="no_found", evidence=[ev("No MCP server is offered")])
    claims.main_blocker = Claim(value=Blocker(text="none", type="none"), evidence=[ev("Self-serve keys for everyone")])
    claims.webhooks = Claim(value="yes", evidence=[ev("Webhooks are supported")])
    return AppRecord(app_id=1, app="Acme", pass_="pass1", claims=claims, errors=["pass1 note"],
                     run=RunInfo(model="m", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), tool_calls=9))


class FakeIndex:
    def __init__(self):
        self.snippets = {}

    async def get_text(self, url):
        t = PAGES.get(url, "")
        return t, ("fake" if t else "none")

    def rendered_text(self, url):
        return ""

    def add_snippet(self, url, s):
        self.snippets[url] = s

    async def render(self, url):
        return PageContent(url, "", "", 0, "n", "browser")


class FakeSearch:
    def __init__(self, urls=(NEW1, NEW2)):
        self.urls, self.queries = list(urls), []

    async def search(self, query, n=8, include_domains=None):
        self.queries.append((query, include_domains))
        return [SearchResult(url=u, title="t", snippet=PAGES[u]) for u in self.urls]


class FakeScrape:
    async def fetch(self, url):
        return PageContent(url, "t", PAGES.get(url, ""), 200, "2026-01-01T00:00:00+00:00", "httpx")


class Harness:
    """verifier policy: verdicts[field] for blocks citing OLD; cand[field] for blocks citing new pages."""

    def __init__(self, monkeypatch, verdicts=None, cand=None, extract=None, search=None):
        self.verdicts, self.cand, self.extract = verdicts or {}, cand or {}, extract or {}
        self.verify_calls, self.extract_calls = [], []
        self.search = search or FakeSearch()
        self.omit: set = set()
        self.raise_exc = None
        monkeypatch.setattr(V, "_make_index", FakeIndex)

        async def live(url, http, cache=None, memo=None):
            return {"status": 200}

        monkeypatch.setattr(loops, "liveness", live)
        monkeypatch.setattr(llm, "verify_claim", self._verify)
        monkeypatch.setattr(llm, "extract_json", self._extract)

    async def _verify(self, prompt, schema):
        self.verify_calls.append(prompt)
        if self.raise_exc is not None:
            raise self.raise_exc
        out = []
        for block in prompt.split("## field: ")[1:]:
            f = block.split("\n", 1)[0].strip()
            if f in self.omit:
                continue
            is_new = "/new" in block
            st = (self.cand if is_new else self.verdicts).get(f, "supported")
            if callable(st):
                st = st(block)
            out.append({"field": f, "status": st, "reason": "r" * 300})
        return schema.model_validate({"verdicts": out}), {"input_tokens": 10, "output_tokens": 5}

    async def _extract(self, system, user, schema):
        name = next(n for n, f in Extraction.model_fields.items() if f.annotation is schema)
        self.extract_calls.append((name, user))
        spec = self.extract.get(name)
        if callable(spec):
            spec = spec(len([c for c in self.extract_calls if c[0] == name]))
        sids = dict((u, s) for s, u in re.findall(r"\[(S\d+)\] url: (\S+)", user))
        if spec is None:
            data = {"citations": [], "confidence": "low", "note": ""}
            data.update({"text": "unknown", "type": "unknown"} if name == "main_blocker" else
                        {"value": ["unknown"] if name in ("auth_methods", "api_type") else "unknown"})
        else:
            value, quote, url = spec
            data = {"citations": [Citation(source_id=sids.get(url, "S1"), quote=quote).model_dump()],
                    "confidence": "high", "note": "n"}
            if name == "main_blocker":
                data.update(value)
            else:
                data["value"] = value
        return schema.model_validate(data), {"input_tokens": 100, "output_tokens": 20}

    def run(self, rec=None):
        rec = rec or base_record()
        return rec, asyncio.run(V.verify_app(rec, APP, (self.search, FakeScrape(), None)))


def l6(claim):
    return [e for e in claim.verification.loops if e.loop == "L6"]


def check_ledger(rec: AppRecord):
    for n in type(rec.claims).model_fields:
        for e in getattr(rec.claims, n).verification.loops:
            vocab = L5_VOCAB if e.loop == "L5" else L6_VOCAB if e.loop == "L6" else L14_VOCAB
            assert e.result in vocab, (n, e)
            if e.loop == "L6" and e.result in ("changed", "set_unknown"):
                left, right = e.detail.split(" -> ", 1)
                json.loads(left)
                json.loads(right.split(" | ", 1)[0])


# ------------------------------------------------------------------ tests
def test_all_supported_no_rerun_and_record_shape(monkeypatch):
    h = Harness(monkeypatch)
    before, out = h.run()
    assert out.pass_ == "pass2" and AppRecord.model_validate(out.model_dump(by_alias=True))
    assert h.search.queries == [] and h.extract_calls == []
    for n in ("auth_methods", "credential_access", "production_gate", "api_type", "mcp_official", "main_blocker",
              "webhooks"):
        c = getattr(out.claims, n)
        assert c.verification.status == "supported" and not l6(c)
        assert [e.loop for e in c.verification.loops][-1] == "L5"
        assert len([e for e in c.verification.loops if e.loop == "L5"][0].detail) <= 200
    assert out.claims.category.verification.status == "supported"
    assert out.claims.description.verification.status == "insufficient_evidence"
    # one batched L5 call for the app; run counters
    assert len(h.verify_calls) == 1
    assert out.run.verifier_tokens_in == 10 and out.run.verifier_tokens_out == 5
    assert out.run.l6_rerun_claims == 0 and out.run.l6_tool_calls == 0 and out.run.tokens_in == 0
    assert out.run.verifier_model and out.errors == ["pass1 note"]
    # pass-1 input not mutated
    assert before.pass_ == "pass1" and all(not getattr(before.claims, n).verification.loops
                                           for n in type(before.claims).model_fields)
    assert before.run.tool_calls == 9
    check_ledger(out)


def test_verifier_prompt_has_no_app_context_and_marks_quote(monkeypatch):
    h = Harness(monkeypatch)
    h.run()
    p = h.verify_calls[0]
    assert '"Acme"' in p and "crm" not in p.lower().replace("acme", "")
    assert ">>>OAuth 2.0 authorization code flow is supported<<<" in p
    assert "## field: category" not in p and "## field: description" not in p


def test_unknown_evaluated_field_rerun_and_changed(monkeypatch):
    rec = base_record()
    rec.claims.credential_access = Claim.model_validate({"value": "unknown"})
    rec.claims.credential_access = type(rec.claims).model_fields["credential_access"].annotation(value="unknown")
    h = Harness(monkeypatch, extract={"credential_access": ("self_serve_free", "API access on the free plan for all", NEW1)})
    _, out = h.run(rec)
    c = out.claims.credential_access
    assert c.value == "self_serve_free" and c.verification.status == "supported"
    assert c.evidence[0].url == NEW1
    e = l6(c)[-1]
    assert e.result == "changed" and e.detail.startswith('"unknown" -> "self_serve_free" | ')
    assert any(e.loop == "L5" and "[L6 attempt 1" in e.detail for e in c.verification.loops)
    assert out.run.l6_rerun_claims == 1 and out.run.tokens_in == 100 and out.run.l6_tool_calls > 0
    q = [q for q, d in h.search.queries]
    assert q[0].startswith("Acme API access pricing plan free developer account")
    assert all(d == ["acme.com"] for _, d in h.search.queries)
    check_ledger(out)


def test_confirmed_when_same_value(monkeypatch):
    h = Harness(monkeypatch, verdicts={"auth_methods": "insufficient_evidence"},
                extract={"auth_methods": (["oauth2"], "use OAuth 2.0", NEW1)})
    _, out = h.run()
    c = out.claims.auth_methods
    assert c.value == ["oauth2"] and c.verification.status == "supported"
    assert l6(c)[-1].result == "confirmed"
    assert {e.url for e in c.evidence} == {OLD, NEW1}


def test_partial_set_replaced_by_new_set_not_union(monkeypatch):
    h = Harness(monkeypatch, verdicts={"auth_methods": "partially_supported"},
                extract={"auth_methods": (["oauth2", "api_key"], "use OAuth 2.0 or an API key header", NEW1)})
    _, out = h.run()
    c = out.claims.auth_methods
    assert c.value == ["oauth2", "api_key"] and c.verification.status == "supported"
    e = l6(c)[-1]
    assert e.result == "changed" and e.detail.startswith('["oauth2"] -> ["oauth2", "api_key"] | ')
    assert "not a union" in e.detail
    check_ledger(out)


def test_contradicted_and_rejected_sets_unknown(monkeypatch):
    search = FakeSearch()

    async def per_attempt(query, n=8, include_domains=None):
        search.queries.append((query, include_domains))
        u = NEW1 if "API reference REST GraphQL" in query or "API endpoints" in query else NEW2
        return [SearchResult(url=u, title="t", snippet=PAGES[u])]

    search.search = per_attempt
    h = Harness(monkeypatch, verdicts={"api_type": "contradicted"}, cand={"api_type": "insufficient_evidence"},
                extract={"api_type": (["graphql"], "Authentication: use OAuth 2.0", NEW1)}, search=search)
    _, out = h.run()
    c = out.claims.api_type
    assert c.is_unknown() and c.value == ["unknown"]
    e = l6(c)[-1]
    assert e.result == "set_unknown" and e.detail.startswith('["rest"] -> ["unknown"] | ')
    assert c.verification.status == "insufficient_evidence"
    assert len([x for x in h.extract_calls if x[0] == "api_type"]) == 2  # two attempts, then stop
    check_ledger(out)


def test_partial_kept_when_candidate_not_supported(monkeypatch):
    h = Harness(monkeypatch, verdicts={"production_gate": "partially_supported"},
                cand={"production_gate": "partially_supported"},
                extract={"production_gate": ("partner_program", "Partner program required to list apps", NEW2)})
    _, out = h.run()
    c = out.claims.production_gate
    assert c.value == "none" and c.verification.status == "partially_supported"
    assert l6(c)[-1].result == "no_change"


def test_unknown_stays_unknown_when_rejected(monkeypatch):
    rec = base_record()
    rec.claims.mcp_official = type(rec.claims).model_fields["mcp_official"].annotation(value="unknown")
    h = Harness(monkeypatch)  # extractor answers unknown
    _, out = h.run(rec)
    c = out.claims.mcp_official
    assert c.is_unknown() and l6(c)[-1].result == "no_change" and "unknown stays unknown" in l6(c)[-1].detail
    q = [q for q, _ in h.search.queries]
    assert any("official MCP server" in x for x in q) and any("remote MCP server" in x for x in q)


def test_second_attempt_accepted_with_new_phrasing(monkeypatch):
    def spec(n):
        return (["graphql"], "Authentication: use OAuth 2.0", NEW1) if n == 1 else \
            (["rest"], "Personal access tokens are sent as Bearer tokens", NEW2)

    search = FakeSearch()

    async def by_attempt(query, n=8, include_domains=None):
        search.queries.append((query, include_domains))
        u = NEW1 if "API reference REST GraphQL" in query or "API endpoints" in query else NEW2
        return [SearchResult(url=u, title="t", snippet=PAGES[u])]

    search.search = by_attempt
    h = Harness(monkeypatch, verdicts={"api_type": "insufficient_evidence"},
                cand={"api_type": lambda b: "supported" if "new2" in b else "contradicted"},
                extract={"api_type": spec}, search=search)
    _, out = h.run()
    c = out.claims.api_type
    assert c.value == ["rest"] and l6(c)[-1].result == "confirmed"
    assert len({q for q, _ in search.queries}) == 4


def test_supported_but_ungrounded_is_insufficient_and_rerun(monkeypatch):
    rec = base_record()
    rec.claims.api_type = type(rec.claims).model_fields["api_type"].annotation(
        value=["rest"], evidence=[ev("a hallucinated quote that is not on the page at all")])
    h = Harness(monkeypatch)  # extractor answers unknown on re-research
    _, out = h.run(rec)
    c = out.claims.api_type
    assert c.verification.status == "insufficient_evidence"
    assert "no quote was grounded" in c.verification.notes
    assert c.value == ["rest"] and l6(c)[-1].result == "no_change"


def test_candidate_never_accepted_without_grounded_quote(monkeypatch):
    rec = base_record()
    rec.claims.credential_access = type(rec.claims).model_fields["credential_access"].annotation(value="unknown")
    h = Harness(monkeypatch, extract={"credential_access": ("paid_plan_required", "invented text not on page", NEW1)})
    _, out = h.run(rec)
    assert out.claims.credential_access.is_unknown()


def test_non_evaluated_contradicted_set_unknown_not_researched(monkeypatch):
    h = Harness(monkeypatch, verdicts={"webhooks": "contradicted"})
    _, out = h.run()
    c = out.claims.webhooks
    assert c.is_unknown()
    e = l6(c)[-1]
    assert e.result == "set_unknown" and e.detail == '"yes" -> "unknown" | contradicted by verifier, not re-researched'
    assert h.search.queries == []


def test_non_evaluated_partial_kept(monkeypatch):
    h = Harness(monkeypatch, verdicts={"webhooks": "partially_supported"})
    _, out = h.run()
    assert out.claims.webhooks.value == "yes" and out.claims.webhooks.verification.status == "partially_supported"
    assert not l6(out.claims.webhooks)


def test_missing_verdict_is_error_not_fabricated(monkeypatch):
    h = Harness(monkeypatch)
    h.omit = {"webhooks", "auth_methods"}
    _, out = h.run()
    w = out.claims.webhooks
    l5 = [e for e in w.verification.loops if e.loop == "L5"]
    assert l5[0].result == "error" and w.verification.status == "insufficient_evidence"
    assert any("no verdict" in e for e in out.errors)
    # evaluated field with an error verdict is re-researched (and its candidate also gets no verdict)
    assert l6(out.claims.auth_methods)


def test_all_verifier_calls_fail_raises(monkeypatch):
    h = Harness(monkeypatch)
    h.raise_exc = RuntimeError("verifier output failed validation")
    with pytest.raises(RuntimeError, match="verifier failed for all"):
        h.run()


def test_fatal_verifier_error_raises(monkeypatch):
    h = Harness(monkeypatch)
    h.raise_exc = RuntimeError("Missing required environment variable: META_API_KEY (set it in .env)")
    with pytest.raises(RuntimeError, match="META_API_KEY"):
        h.run()


def test_l5_chunks_large_prompts(monkeypatch):
    monkeypatch.setattr(V, "L5_CHUNK_CHARS", 10)
    h = Harness(monkeypatch)
    _, out = h.run()
    assert len(h.verify_calls) == 7  # one per claim when each block exceeds the limit
    assert out.run.verifier_tokens_in == 70


def test_main_blocker_uses_verified_context(monkeypatch):
    rec = base_record()
    rec.claims.main_blocker = type(rec.claims).model_fields["main_blocker"].annotation(
        value=Blocker(text="unknown", type="unknown"))
    h = Harness(monkeypatch, extract={"main_blocker": ({"text": "partner program to list apps", "type": "access"},
                                                       "Partner program required to list apps", NEW2)})
    _, out = h.run(rec)
    c = out.claims.main_blocker
    assert c.value.type == "access" and l6(c)[-1].result == "changed"
    assert l6(c)[-1].detail.startswith('{"text": "unknown", "type": "unknown"} -> {"text": "partner program')
    user = [u for n, u in h.extract_calls if n == "main_blocker"][0]
    assert "credential_access = \"self_serve_free\" (supported)" in user
    assert [q for q, _ in h.search.queries][0] == "Acme API limitations restrictions access requirements"


def test_locate_quote_and_excerpt_window():
    text = "x " * 2000 + "The **REST** API   uses JSON." + " y" * 2000
    span = V.locate_quote("The REST API uses JSON.", text)
    assert span is not None
    ex, found = V.excerpt_for("api_type", ev("The REST API uses JSON."), text)
    assert found and ">>>" in ex and len(ex) <= 2 * V.EXCERPT_WINDOW + 60
    ex2, found2 = V.excerpt_for("webhooks", ev("absent words entirely here"), "a " * 3000 + "webhook events" + " b" * 3000)
    assert not found2 and "webhook" in ex2
