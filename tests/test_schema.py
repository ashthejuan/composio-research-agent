import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agent.schema import (
    AppRecord, Blocker, Buildability, Claim, Claims, ComposioToolkit,
    DimensionScore, GoldenLabel, RunInfo, ScoreBreakdown, dump_record, load_record,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run():
    return RunInfo(model="m", started_at=NOW, backend="test")


def ev(tier="official_docs"):
    return {"url": "https://developers.example.com/docs", "quote": "placeholder quote",
            "source_tier": tier}


def claim(value):
    return {"value": value, "evidence": [ev()], "extractor_confidence": "high"}


def test_all_unknown_roundtrip(tmp_path):
    rec = AppRecord.unknown(5, "Foo", "pass1", run())
    p = tmp_path / "r.json"
    dump_record(rec, p)
    assert load_record(p) == rec
    assert json.loads(p.read_text())["pass"] == "pass1"


def test_realistic_record(tmp_path):
    d = {
        "app_id": 2, "app": "HubSpot", "pass": "pass1",
        "claims": {
            "category": claim("CRM and Sales"), "description": claim("CRM platform"),
            "auth_methods": claim(["oauth2", "bearer_token"]),
            "credential_access": claim("self_serve_free"),
            "production_gate": claim("marketplace_listing"),
            "api_type": claim(["rest", "graphql"]),
            "api_breadth": claim("broad"),
            "mcp_official": claim("yes"),
            "mcp_third_party": {"value": "unknown"},
            "free_tier_or_sandbox": {**claim("yes"), "note": "dev test account"},
            "webhooks": claim("yes"),
            "official_sdks": claim(["python", "node"]),
            "rate_limit_note": claim("100 req / 10s"),
            "open_source_self_host": claim("no"),
            "main_blocker": claim({"text": "none", "type": "none"}),
            "docs_url": claim("https://developers.example.com"),
        },
        "composio_toolkit": {"value": "available", "slug": "hubspot",
                             "source": "composio_catalog"},
        "buildability": {"score": 0, "verdict": "unknown", "bucket": "unknown",
                         "commercial_gate": False},
        "record_confidence": "medium",
        "run": {"model": "m", "started_at": NOW.isoformat(), "tool_calls": 3},
    }
    rec = AppRecord.model_validate(d)
    p = tmp_path / "h.json"
    dump_record(rec, p)
    assert "pass" in json.loads(p.read_text()) and "pass_" not in json.loads(p.read_text())
    assert load_record(p) == rec


def test_missing_evidence_raises():
    with pytest.raises(ValidationError):
        Claim[str](value="CRM")
    assert Claim[str](value="unknown").is_unknown()


def test_mixed_unknown_raises():
    c = Claims.all_unknown().model_dump()
    c["auth_methods"] = claim(["unknown", "oauth2"])
    with pytest.raises(ValidationError):
        Claims.model_validate(c)


def _bd(scores):
    names = ["api_availability", "credential_accessibility", "documentation_quality",
             "agent_friendly_operations", "tooling_readiness"]
    return ScoreBreakdown(**{n: DimensionScore(score=s, driver="x")
                             for n, s in zip(names, scores)})


def test_buildability_sum():
    Buildability(score=7, verdict="moderate", breakdown=_bd([2, 2, 1, 1, 1]))
    with pytest.raises(ValidationError):
        Buildability(score=9, verdict="easy", breakdown=_bd([2, 2, 1, 1, 1]))
    with pytest.raises(ValidationError):
        DimensionScore(score=3, driver="x")


def test_golden_label():
    f = {"value": "oauth2", "url": "https://x.example.com/d", "quote": "q"}
    fields = {k: f for k in ["auth_methods", "credential_access", "production_gate",
                             "api_type", "mcp_official", "main_blocker_type"]}
    fields["api_type"] = {"status": "ambiguous", "note": "unclear"}
    g = GoldenLabel(app_id=1, app="X", labeller="me", labelled_at=NOW, fields=fields)
    assert g.fields["api_type"].status == "ambiguous"
    with pytest.raises(ValidationError):
        GoldenLabel(app_id=1, app="X", labeller="me", labelled_at=NOW, fields={})


def test_category_constrained_to_registry():
    from agent.registry import CATEGORIES
    from agent.schema import Claims

    assert Claims.all_unknown().category.is_unknown()
    base = Claims.all_unknown().model_dump()
    base["category"] = claim("CRM")
    with pytest.raises(ValidationError):
        Claims.model_validate(base)
    base["category"] = claim(CATEGORIES[0])
    assert Claims.model_validate(base).category.value == CATEGORIES[0]


def test_settings_repr_hides_keys():
    from agent.config import Settings

    s = Settings(composio_api_key="sekret1", openai_api_key="sekret2",
                 meta_api_key="sekret3", exa_api_key="sekret4")
    assert "sekret" not in repr(s)


def test_all_unknown_claims_are_distinct_objects():
    from agent.schema import Claims
    c = Claims.all_unknown()
    assert c.description is not c.rate_limit_note
    assert c.description is not c.docs_url
    c.description.note = "x"
    assert c.docs_url.note == ""
