"""Unit tests for cross-app analytics, templated headlines, and sanity checks (FR-09 / FR-10)."""
from __future__ import annotations

from datetime import datetime, timezone

from agent import analytics
from agent.schema import (
    AppRecord,
    Blocker,
    Buildability,
    Claim,
    Claims,
    ComposioToolkit,
    DimensionScore,
    Evidence,
    RunInfo,
    ScoreBreakdown,
)


def _ev():
    return [Evidence(url="https://docs.example.com", quote="Example quote", source_tier="official_docs")]


def _bd(scores: tuple[int, int, int, int, int]) -> ScoreBreakdown:
    a, b, c, d, e = scores
    return ScoreBreakdown(
        api_availability=DimensionScore(score=a, driver="a"),
        credential_accessibility=DimensionScore(score=b, driver="b"),
        documentation_quality=DimensionScore(score=c, driver="c"),
        agent_friendly_operations=DimensionScore(score=d, driver="d"),
        tooling_readiness=DimensionScore(score=e, driver="e"),
    )


def _make_record(
    app_id: int,
    name: str,
    category: str,
    *,
    auth=("oauth2",),
    access="self_serve_free",
    gate="none",
    api_type=("rest",),
    mcp_off="yes",
    composio="available",
    blocker_type="access",
    scores=(2, 2, 2, 2, 2),
    verdict="easy",
    commercial_gate=False,
    bucket="easy_win",
) -> AppRecord:
    c = Claims.all_unknown()
    c.category = Claim(value=category, evidence=_ev())
    c.auth_methods = Claim(value=list(auth), evidence=_ev())
    c.credential_access = Claim(value=access, evidence=_ev())
    c.production_gate = Claim(value=gate, evidence=_ev())
    c.api_type = Claim(value=list(api_type), evidence=_ev())
    c.mcp_official = Claim(value=mcp_off, evidence=_ev())
    c.main_blocker = Claim(value=Blocker(text=f"{blocker_type} issue", type=blocker_type), evidence=_ev())
    return AppRecord(
        app_id=app_id,
        app=name,
        pass_="final",
        claims=c,
        composio_toolkit=ComposioToolkit(value=composio, slug=name.lower() if composio == "available" else None),
        buildability=Buildability(
            score=sum(scores),
            verdict=verdict,
            breakdown=_bd(scores),
            commercial_gate=commercial_gate,
            bucket=bucket,
        ),
        run=RunInfo(model="m", started_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )


def _sample_records() -> list[AppRecord]:
    return [
        _make_record(
            1, "Alpha", "CRM and Sales",
            auth=("oauth2", "api_key"), access="self_serve_free", gate="oauth_app_review",
            mcp_off="yes", composio="unavailable", blocker_type="access",
            scores=(2, 2, 2, 2, 2), verdict="easy", commercial_gate=False, bucket="easy_win",
        ),
        _make_record(
            2, "Beta", "CRM and Sales",
            auth=("oauth2",), access="contact_sales", gate="partner_program",
            mcp_off="no_found", composio="unavailable", blocker_type="commercial",
            scores=(2, 0, 2, 2, 0), verdict="moderate", commercial_gate=True, bucket="needs_outreach",
        ),
        _make_record(
            3, "Gamma", "Ecommerce",
            auth=("api_key",), access="paid_plan_required", gate="none",
            mcp_off="yes", composio="available", blocker_type="access",
            scores=(1, 1, 2, 1, 2), verdict="moderate", commercial_gate=False, bucket="moderate",
        ),
        _make_record(
            4, "Delta", "Finance and Fintech",
            auth=("none",), access="unknown", gate="unknown", api_type=("none_documented",),
            mcp_off="no_found", composio="unavailable", blocker_type="technical",
            scores=(0, 0, 0, 0, 0), verdict="blocked", commercial_gate=False, bucket="technically_blocked",
        ),
    ]


def test_analytics_distributions_matrices_and_sanity(tmp_path):
    res = analytics.compute_analytics(_sample_records(), reports_dir=tmp_path)
    assert res["sanity_checks"]["all_passed"] is True
    assert res["sanity_checks"]["bucket_sum"] == 4
    assert res["summary"]["easy_wins_without_composio"] == 1
    assert res["distributions"]["auth_methods"]["multi_method_apps"] == 1
    assert res["distributions"]["production_gate"]["gated_despite_free_dev"] == 1
    assert res["matrices"]["category_x_credential_access"]["CRM and Sales"]["self_serve_free"] == 1
    assert res["matrices"]["category_x_credential_access"]["CRM and Sales"]["contact_sales"] == 1
    assert len(res["mcp_vs_composio"]["official_mcp_without_composio"]) == 1
    assert res["mcp_vs_composio"]["official_mcp_without_composio"][0]["app"] == "Alpha"
    assert res["holdout"]["status"] == "skipped_per_prd_cut_line_1"
    assert len(res["headlines"]) >= 5


def test_headlines_update_dynamically_from_data(tmp_path):
    recs = _sample_records()
    h1 = {h["id"]: h["text"] for h in analytics.compute_analytics(recs, reports_dir=tmp_path)["headlines"]}
    recs[0].composio_toolkit = ComposioToolkit(value="available", slug="alpha")
    h2 = {h["id"]: h["text"] for h in analytics.compute_analytics(recs, reports_dir=tmp_path)["headlines"]}
    assert h1["easy_wins_gap"] != h2["easy_wins_gap"]
    assert "0 of those" in h2["easy_wins_gap"]


def test_sanity_check_catches_bad_bucket(tmp_path):
    recs = _sample_records()
    recs[1].buildability.bucket = "moderate"  # wrong: commercial_gate=True must be needs_outreach
    res = analytics.compute_analytics(recs, reports_dir=tmp_path)
    assert res["sanity_checks"]["all_passed"] is False
    assert 2 in res["sanity_checks"]["bucket_precedence_mismatches"]
