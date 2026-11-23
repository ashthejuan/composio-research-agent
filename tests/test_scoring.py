"""Unit tests for deterministic buildability scoring (FR-04), buckets (FR-10), and build_final."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from agent import scoring
from agent.registry import App
from agent.schema import (
    AppRecord,
    Blocker,
    Claim,
    Claims,
    ComposioToolkit,
    Evidence,
    GoldenField,
    GoldenLabel,
    RunInfo,
    dump_record,
    load_record,
)


def _ev(url="https://docs.example.com/api", quote="API documentation", tier="official_docs"):
    return [Evidence(url=url, quote=quote, source_tier=tier)]


def _rec(
    app_id=1,
    name="Acme",
    *,
    api_type=("rest",),
    api_breadth="broad",
    credential_access="self_serve_free",
    production_gate="none",
    auth_methods=("oauth2",),
    docs_url="https://docs.example.com",
    docs_tier="official_docs",
    webhooks="yes",
    mcp_official="yes",
    mcp_third_party="no_found",
    official_sdks=("python",),
    composio="unavailable",
) -> AppRecord:
    c = Claims.all_unknown()
    c.category = Claim(value="CRM and Sales", evidence=_ev())
    c.api_type = Claim(value=list(api_type), evidence=_ev() if api_type != ("unknown",) else [])
    c.api_breadth = Claim(value=api_breadth, evidence=_ev() if api_breadth != "unknown" else [])
    c.credential_access = Claim(
        value=credential_access, evidence=_ev() if credential_access != "unknown" else []
    )
    c.production_gate = Claim(
        value=production_gate, evidence=_ev() if production_gate != "unknown" else []
    )
    c.auth_methods = Claim(
        value=list(auth_methods), evidence=_ev(tier=docs_tier) if auth_methods != ("unknown",) else []
    )
    c.docs_url = Claim(
        value=docs_url, evidence=_ev(tier=docs_tier) if docs_url != "unknown" else []
    )
    c.webhooks = Claim(value=webhooks, evidence=_ev() if webhooks != "unknown" else [])
    c.mcp_official = Claim(value=mcp_official, evidence=_ev() if mcp_official != "unknown" else [])
    c.mcp_third_party = Claim(
        value=mcp_third_party, evidence=_ev() if mcp_third_party != "unknown" else []
    )
    c.official_sdks = Claim(
        value=list(official_sdks), evidence=_ev() if official_sdks != ("unknown",) else []
    )
    c.main_blocker = Claim(value=Blocker(text="none", type="none"), evidence=_ev())
    return AppRecord(
        app_id=app_id,
        app=name,
        pass_="pass2",
        claims=c,
        composio_toolkit=ComposioToolkit(value=composio, slug="acme" if composio == "available" else None),
        run=RunInfo(model="m", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )


def test_perfect_easy_win_scores_10():
    b = scoring.score_record(_rec())
    assert b.score == 10
    assert b.verdict == "easy"
    assert b.commercial_gate is False
    assert b.bucket == "easy_win"
    assert b.breakdown is not None
    assert b.breakdown.total() == 10
    assert all(getattr(b.breakdown, f).driver for f in type(b.breakdown).model_fields)


def test_commercial_gate_overrides_high_score():
    r_partner = _rec(production_gate="partner_program")
    b1 = scoring.score_record(r_partner)
    assert b1.score == 10
    assert b1.commercial_gate is True
    assert b1.bucket == "needs_outreach"

    r_sales = _rec(credential_access="contact_sales")
    b2 = scoring.score_record(r_sales)
    assert b2.score == 8
    assert b2.commercial_gate is True
    assert b2.bucket == "needs_outreach"


def test_oss_cli_tool_scoring():
    app = App(
        id=58, name="Sherlock", category="Data, SEO and Scraping",
        website="https://github.com/sherlock-project/sherlock",
        hint="cli", official_domains=["github.com/sherlock-project"], kind="oss_tool",
    )
    r = _rec(
        app_id=58, name="Sherlock", api_type=("cli",), api_breadth="narrow",
        credential_access="unknown", auth_methods=("none",), webhooks="unknown",
        mcp_official="no_found", mcp_third_party="yes", official_sdks=("unknown",),
    )
    b = scoring.score_record(r, app)
    assert b.breakdown.api_availability.score == 1
    assert b.breakdown.credential_accessibility.score == 2
    assert b.breakdown.documentation_quality.score == 2
    assert b.breakdown.agent_friendly_operations.score == 1
    assert b.breakdown.tooling_readiness.score == 1
    assert b.score == 7
    assert b.verdict == "moderate"
    assert b.bucket == "moderate"


def test_blocked_app_scores_low_and_technically_blocked():
    r = _rec(
        api_type=("none_documented",), api_breadth="unknown", credential_access="unknown",
        auth_methods=("unknown",), docs_url="unknown", webhooks="unknown",
        mcp_official="no_found", mcp_third_party="no_found", official_sdks=("unknown",),
    )
    b = scoring.score_record(r)
    assert b.score == 0
    assert b.verdict == "blocked"
    assert b.commercial_gate is False
    assert b.bucket == "technically_blocked"


def test_dimension_middle_tiers():
    r = _rec(
        api_type=("rest",), api_breadth="medium", credential_access="paid_plan_required",
        docs_tier="community", webhooks="no", mcp_official="no_found",
        mcp_third_party="no_found", official_sdks=("python",),
    )
    b = scoring.score_record(r)
    assert b.breakdown.api_availability.score == 2
    assert b.breakdown.credential_accessibility.score == 1
    assert b.breakdown.documentation_quality.score == 1
    assert b.breakdown.agent_friendly_operations.score == 1
    assert b.breakdown.tooling_readiness.score == 1
    assert b.score == 6
    assert b.verdict == "moderate"
    assert b.bucket == "moderate"


def test_build_final_applies_golden_corrections_and_preserves_pass2(tmp_path):
    p2_dir = tmp_path / "pass2"
    fin_dir = tmp_path / "final"
    g_dir = tmp_path / "golden"
    p2_dir.mkdir()
    g_dir.mkdir()

    r1 = _rec(app_id=2, name="HubSpot", credential_access="unknown", production_gate="unknown")
    r2 = _rec(app_id=3, name="Pipedrive", credential_access="self_serve_free")
    dump_record(r1, p2_dir / "002-hubspot.json")
    dump_record(r2, p2_dir / "003-pipedrive.json")

    label = GoldenLabel(
        app_id=2,
        app="HubSpot",
        labeller="human",
        labelled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        fields={
            "auth_methods": GoldenField(status="labelled", value=["oauth2"], url="https://hubspot.com/auth", quote="OAuth2"),
            "credential_access": GoldenField(status="labelled", value="self_serve_free", url="https://hubspot.com/dev", quote="Free dev account"),
            "production_gate": GoldenField(status="labelled", value="marketplace_listing", url="https://hubspot.com/mkt", quote="Marketplace listing"),
            "api_type": GoldenField(status="labelled", value=["rest"], url="https://hubspot.com/api", quote="REST API"),
            "mcp_official": GoldenField(status="labelled", value="yes", url="https://hubspot.com/mcp", quote="Official MCP"),
            "main_blocker_type": GoldenField(status="labelled", value="none", url="https://hubspot.com/dev", quote="No blocker"),
        },
    )
    (g_dir / "002.json").write_text(label.model_dump_json(indent=2))

    corr_path = tmp_path / "corrections.json"
    summary = scoring.build_final(
        pass2_dir=p2_dir, final_dir=fin_dir, golden_dir=g_dir, corrections_out=corr_path, apps=[],
    )
    assert summary["total_records"] == 2
    assert summary["corrected_apps"] == 1
    assert summary["total_corrections"] == 2  # credential_access + production_gate

    # pass2 untouched
    orig = load_record(p2_dir / "002-hubspot.json")
    assert orig.pass_ == "pass2" and orig.claims.credential_access.value == "unknown"

    # final corrected and scored
    fin = load_record(fin_dir / "002-hubspot.json")
    assert fin.pass_ == "final"
    assert fin.claims.credential_access.value == "self_serve_free"
    assert fin.claims.production_gate.value == "marketplace_listing"
    assert fin.buildability.score == 10 and fin.buildability.bucket == "easy_win"

    # corrections.json written with before -> after diffs
    corr = json.loads(corr_path.read_text())
    assert len(corr["corrections"]) == 2
    fields = {c["field"] for c in corr["corrections"]}
    assert fields == {"credential_access", "production_gate"}
