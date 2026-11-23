import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from agent import loops as L
from agent.cache import DiskCache
from agent.registry import App
from agent.research import REGISTRY_NOTE, REGISTRY_QUOTE_PREFIX
from agent.schema import AppRecord, Blocker, Claim, Claims, Evidence, RunInfo
from agent.tools.base import PageContent

URL = "https://docs.acme.com/api"
APP = App(id=1, name="Acme", category="CRM and Sales", website="https://acme.com", hint="h",
          official_domains=["acme.com"])


def ev(quote="OAuth 2.0 authorization is supported", url=URL, tier="official_docs"):
    return Evidence(url=url, quote=quote, source_tier=tier)


def C(value, *evs):
    return Claim(value=value, evidence=list(evs))


def record(**over):
    c = Claims.all_unknown().model_dump()
    claims = Claims.model_validate(c)
    for k, v in over.items():
        setattr(claims, k, v)
    return AppRecord(app_id=1, app="Acme", pass_="pass1", claims=claims,
                     run=RunInfo(model="m", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))


class FakeIndex:
    def __init__(self, text="", rendered=""):
        self.text, self.rendered, self.rendered_calls = text, rendered, 0

    async def get_text(self, url):
        return self.text, ("fake" if self.text else "none")

    async def render(self, url):
        self.rendered_calls += 1
        return PageContent(url, "", self.rendered, 200 if self.rendered else 0, "n", "browser")


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------- normalise
def test_normalise_cases():
    n = L.normalise
    assert n("It’s “fine”") == 'it\'s "fine"'
    assert n("A   B\n\tC") == "a b c"
    assert n("| `Auth` | **OAuth 2.0** |") == n("Auth OAuth 2.0")


def test_l3_exact_markdown_row_and_whitespace():
    e = ev("Auth: OAuth 2.0   (authorization code)")
    r, nr = run(L.l3_ground("auth_methods", e, FakeIndex("| Auth: | **OAuth 2.0** | (authorization\ncode) |")))
    assert r.result == "pass" and not nr and "exact" in r.detail


def test_l3_fuzzy_boundary():
    base = "the quick brown fox jumps over the lazy dog near the river bank today"
    text = "intro. " + base + " outro."
    ok = base[:-1] + "x"  # tiny typo
    bad = "completely different sentence about unrelated pricing structures and plans"
    assert run(L.l3_ground("api_type", ev(ok), FakeIndex(text)))[0].result == "pass"
    r, nr = run(L.l3_ground("api_type", ev(bad), FakeIndex(text)))
    assert r.result == "fail" and nr is False


def test_l3_fuzzy_threshold_boundary(monkeypatch):
    for score, want in ((89.9, "fail"), (90.0, "pass")):
        monkeypatch.setattr(L.fuzz, "partial_ratio", lambda a, b, s=score: s)
        assert run(L.l3_ground("api_type", ev("not literally present"), FakeIndex("some page")))[0].result == want


def test_l3_needs_render_on_empty():
    r, nr = run(L.l3_ground("api_type", ev("x"), FakeIndex("")))
    assert r.result == "fail" and nr is True


def test_l3_docs_url_special():
    e = ev(quote="https://docs.acme.com/api/", url="https://www.docs.acme.com/api")
    assert run(L.l3_ground("docs_url", e, FakeIndex("")))[0].result == "pass"
    e2 = ev(quote="https://docs.acme.com/other", url=URL)
    assert run(L.l3_ground("docs_url", e2, FakeIndex("see https://docs.acme.com/other here")))[0].result == "pass"
    assert run(L.l3_ground("docs_url", e2, FakeIndex("nothing")))[0].result == "fail"
    # the special case does not apply to other fields
    assert run(L.l3_ground("api_type", e, FakeIndex("")))[1] is True


def test_l3_registry_skip():
    e = ev(quote=REGISTRY_QUOTE_PREFIX + "CRM")
    r, nr = run(L.l3_ground("category", e, FakeIndex("")))
    assert r.result == "skip" and not nr


def test_l4_render_then_ground():
    e = ev("hello world")
    assert run(L.l4_render_then_ground("api_type", e, FakeIndex(rendered="say Hello   World!"))).result == "pass"
    assert run(L.l4_render_then_ground("api_type", e, FakeIndex(rendered="nothing relevant at all here"))).result == "fail"
    assert run(L.l4_render_then_ground("api_type", e, FakeIndex(rendered=""))).result == "error"


# -------------------------------------------------------------------- L1
def l1(rec, field):
    return [(r.result, r.detail) for r in L.l1_rules(rec)[field]]


def test_l1_unknown_skips_and_schema_pass():
    rec = record()
    assert all(v[0].result == "skip" for v in L.l1_rules(rec).values())
    rec = record(api_type=C(["rest"], ev("REST API")))
    assert L.l1_rules(rec)["api_type"][0].result == "pass"


def test_l1_oauth_rule():
    assert L.l1_rules(record(auth_methods=C(["oauth2"], ev("uses oauth")))) ["auth_methods"][-1].result == "pass"
    assert L.l1_rules(record(auth_methods=C(["oauth2"], ev("api keys only"))))["auth_methods"][-1].result == "fail"


def test_l1_access_vs_blocker():
    rec = record(credential_access=C("partner_gated", ev("partners only")),
                 main_blocker=C(Blocker(text="none", type="none"), ev("no blocker")))
    r = L.l1_rules(rec)
    assert r["credential_access"][-1].result == "fail" and r["main_blocker"][-1].result == "fail"
    rec = record(credential_access=C("contact_sales", ev("sales")),
                 main_blocker=C(Blocker(text="sales", type="commercial"), ev("sales")))
    assert L.l1_rules(rec)["main_blocker"][-1].result == "pass"


def test_l1_mcp_gate_graphql_description():
    assert L.l1_rules(record(mcp_official=C("yes", ev("mcp"))))["mcp_official"][-1].result == "pass"
    r = L.l1_rules(record(production_gate=C("oauth_app_review", ev("review", tier="article"))))
    assert r["production_gate"][-1].result == "fail"
    r = L.l1_rules(record(production_gate=C("oauth_app_review", ev("review"))))
    assert r["production_gate"][-1].result == "pass"
    assert L.l1_rules(record(production_gate=C("none", ev("x", tier="article"))))["production_gate"][-1].result == "pass"
    assert L.l1_rules(record(api_type=C(["graphql"], ev("REST only"))))["api_type"][-1].result == "fail"
    assert L.l1_rules(record(api_type=C(["graphql"], ev("GraphQL API"))))["api_type"][-1].result == "pass"
    r = L.l1_rules(record(docs_url=C(URL, ev(URL))))
    assert r["description"][-1].result == "fail"
    r = L.l1_rules(record(docs_url=C(URL, ev(URL)), description=C("A CRM", ev("A CRM"))))
    assert r["description"][-1].result == "pass"


# -------------------------------------------------------------------- L2
def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def l2(e, handler, tmp_path, app=APP):
    async def go():
        async with client(handler) as http:
            return await L.l2_source(app, "api_type", e, http, cache=DiskCache(tmp_path, refresh=False))

    return run(go())


def test_l2_statuses(tmp_path):
    ok = lambda req: httpx.Response(200)
    assert l2(ev(), ok, tmp_path).result == "pass"
    r = l2(ev(), lambda req: httpx.Response(403), tmp_path / "b")
    assert r.result == "pass" and r.detail == "blocked 403"
    assert l2(ev(), lambda req: httpx.Response(404), tmp_path / "c").result == "fail"
    assert l2(ev(), lambda req: httpx.Response(500), tmp_path / "d").result == "error"


def test_l2_redirect_and_head_fallback(tmp_path):
    seen = []

    def h(req):
        seen.append(req.method)
        if req.url.path == "/api":
            return httpx.Response(301, headers={"location": "https://docs.acme.com/new"})
        return httpx.Response(405 if req.method == "HEAD" else 200)

    assert l2(ev(), h, tmp_path).result == "pass"
    assert seen == ["HEAD", "HEAD", "GET", "GET"] or "GET" in seen


def test_l2_dns_error_and_timeout(tmp_path):
    def dns(req):
        raise httpx.ConnectError("name resolution failed")

    assert l2(ev(), dns, tmp_path).result == "fail"

    def to(req):
        raise httpx.ReadTimeout("slow")

    assert l2(ev(), to, tmp_path / "t").result == "error"


def test_l2_official_domain_and_registry(tmp_path):
    e = ev(url="https://evil.example.org/docs", tier="official_docs")
    r = l2(e, lambda req: httpx.Response(200), tmp_path)
    assert r.result == "fail" and "official_domains" in r.detail
    e = ev(url="https://evil.example.org/docs", tier="article")
    assert l2(e, lambda req: httpx.Response(200), tmp_path / "x").result == "pass"
    reg = ev(quote=REGISTRY_QUOTE_PREFIX + "CRM")
    assert l2(reg, lambda req: httpx.Response(404), tmp_path / "y").result == "skip"


def test_l2_caches_liveness(tmp_path):
    n = []

    def h(req):
        n.append(1)
        return httpx.Response(200)

    l2(ev(), h, tmp_path)
    l2(ev(), h, tmp_path)
    assert len(n) == 1


# ------------------------------------------------------------ run_l1_to_l4
def test_run_l1_to_l4_aggregates_and_l4_fix(tmp_path):
    good = ev("REST API")
    other = ev("totally absent quote xyz", url="https://docs.acme.com/other")
    rec = record(api_type=C(["rest"], other, good),
                 webhooks=C("yes", ev("webhooks supported", url="https://docs.acme.com/hooks")),
                 category=Claim(value="CRM and Sales", note=REGISTRY_NOTE,
                                evidence=[ev(REGISTRY_QUOTE_PREFIX + "CRM and Sales", url="https://acme.com/")]))

    class Idx(FakeIndex):
        async def get_text(self, url):
            if url.endswith("/hooks"):
                return "", "none"
            return "intro: a REST API is available", "fake"

    idx = Idx(rendered="Webhooks supported here")

    async def go():
        async with client(lambda req: httpx.Response(200)) as http:
            return await L.run_l1_to_l4(rec, APP, idx, http)

    res = run(go())
    by = {f: {r.loop: r.result for r in lst} for f, lst in res.items()}
    assert by["api_type"]["L2"] == "pass" and by["api_type"]["L3"] == "pass" and "L4" not in by["api_type"]
    assert by["webhooks"]["L3"] == "fail" and by["webhooks"]["L4"] == "pass"
    assert by["category"]["L3"] == "skip"
    assert by["auth_methods"] == {"L1": "skip"}
    assert "per" not in res["api_type"][-1].detail and "[0]" in res["api_type"][-1].detail
