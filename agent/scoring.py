"""Deterministic buildability scoring (FR-04), opportunity buckets (FR-10), and final dataset builder.

Zero LLM involvement: every score, breakdown, verdict, commercial gate, and bucket is computed
deterministically from the verified claims.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent import accuracy
from agent import golden as G
from agent import registry
from agent import research as R
from agent.schema import (
    GOLDEN_FIELDS,
    UNKNOWN,
    AppRecord,
    Blocker,
    Buildability,
    DimensionScore,
    Evidence,
    ScoreBreakdown,
    dump_record,
    load_record,
)

OFFICIAL_TIERS = frozenset({"official_docs", "official_help", "official_github"})
COMMERCIAL_ACCESS = frozenset({"partner_gated", "contact_sales"})
TRIAL_OR_PAID_ACCESS = frozenset({"paid_plan_required", "admin_approval", "self_serve_trial"})


def _has_official_evidence(claim) -> bool:
    return any(e.source_tier in OFFICIAL_TIERS for e in claim.evidence)


def _score_api_availability(record: AppRecord, app: Optional[registry.App] = None) -> DimensionScore:
    api_types = list(record.claims.api_type.value)
    breadth = record.claims.api_breadth.value
    concrete = [t for t in api_types if t not in (UNKNOWN, "none_documented")]
    if not concrete:
        return DimensionScore(score=0, driver=f"api_type={api_types} (none documented)")
    if {"rest", "graphql"} & set(concrete) and breadth in ("medium", "broad"):
        return DimensionScore(score=2, driver=f"api_type={concrete}, api_breadth={breadth}")
    if concrete == ["cli"] and app is not None and app.kind in ("oss_tool", "cli"):
        return DimensionScore(score=1, driver=f"api_type=['cli'], kind={app.kind} (wrappable CLI)")
    return DimensionScore(score=1, driver=f"api_type={concrete}, api_breadth={breadth}")


def _score_credential_accessibility(record: AppRecord) -> DimensionScore:
    access = record.claims.credential_access.value
    auth = list(record.claims.auth_methods.value)
    if access == "self_serve_free":
        return DimensionScore(score=2, driver="credential_access=self_serve_free")
    if auth == ["none"] and access == UNKNOWN:
        return DimensionScore(score=2, driver="auth_methods=['none'] (no credentials needed)")
    if access in TRIAL_OR_PAID_ACCESS:
        return DimensionScore(score=1, driver=f"credential_access={access}")
    return DimensionScore(score=0, driver=f"credential_access={access}")


def _score_documentation_quality(record: AppRecord) -> DimensionScore:
    docs = record.claims.docs_url
    auth = record.claims.auth_methods
    if docs.is_unknown():
        return DimensionScore(score=0, driver="docs_url=unknown")
    has_official = _has_official_evidence(docs) or _has_official_evidence(auth)
    if not auth.is_unknown() and has_official:
        return DimensionScore(score=2, driver="docs_url known + official auth guide")
    why = "auth_methods=unknown" if auth.is_unknown() else "no official-tier evidence"
    return DimensionScore(score=1, driver=f"docs_url known ({why})")


def _score_agent_friendly_operations(record: AppRecord) -> DimensionScore:
    api_types = list(record.claims.api_type.value)
    concrete = [t for t in api_types if t not in (UNKNOWN, "none_documented")]
    breadth = record.claims.api_breadth.value
    webhooks = record.claims.webhooks.value
    if not concrete:
        return DimensionScore(score=0, driver=f"api_type={api_types} (no actionable API)")
    if breadth == "broad":
        return DimensionScore(score=2, driver=f"api_breadth=broad, webhooks={webhooks}")
    if breadth == "medium" and webhooks == "yes":
        return DimensionScore(score=2, driver="api_breadth=medium, webhooks=yes")
    return DimensionScore(score=1, driver=f"api_breadth={breadth}, webhooks={webhooks}")


def _score_tooling_readiness(record: AppRecord) -> DimensionScore:
    mcp_off = record.claims.mcp_official.value
    mcp_3p = record.claims.mcp_third_party.value
    sdks = record.claims.official_sdks
    if mcp_off == "yes":
        return DimensionScore(score=2, driver="mcp_official=yes")
    if not sdks.is_unknown() or mcp_3p == "yes":
        parts = []
        if not sdks.is_unknown():
            parts.append(f"official_sdks={len(sdks.value)}")
        if mcp_3p == "yes":
            parts.append("mcp_third_party=yes")
        return DimensionScore(score=1, driver=", ".join(parts))
    return DimensionScore(score=0, driver="mcp_official!=yes, no official SDK or 3p MCP")


def verdict_for_score(score: int) -> str:
    if score >= 8:
        return "easy"
    if score >= 5:
        return "moderate"
    if score >= 3:
        return "difficult"
    return "blocked"


def has_commercial_gate(record: AppRecord) -> bool:
    return (
        record.claims.credential_access.value in COMMERCIAL_ACCESS
        or record.claims.production_gate.value == "partner_program"
    )


def bucket_for(score: int, commercial_gate: bool) -> str:
    """FR-10 strict precedence: commercial gate overrides score."""
    if commercial_gate:
        return "needs_outreach"
    if score >= 8:
        return "easy_win"
    if score >= 5:
        return "moderate"
    return "technically_blocked"


def score_record(record: AppRecord, app: Optional[registry.App] = None) -> Buildability:
    """Compute FR-04 Buildability + FR-10 bucket deterministically from a record's claims."""
    breakdown = ScoreBreakdown(
        api_availability=_score_api_availability(record, app),
        credential_accessibility=_score_credential_accessibility(record),
        documentation_quality=_score_documentation_quality(record),
        agent_friendly_operations=_score_agent_friendly_operations(record),
        tooling_readiness=_score_tooling_readiness(record),
    )
    total = breakdown.total()
    gate = has_commercial_gate(record)
    return Buildability(
        score=total,
        verdict=verdict_for_score(total),  # type: ignore[arg-type]
        breakdown=breakdown,
        commercial_gate=gate,
        bucket=bucket_for(total, gate),  # type: ignore[arg-type]
    )


def _clean_blocker_text(gf) -> str:
    """Prefer the primary-source quote over internal labeller deliberation notes, truncated at word boundary."""
    if gf.value == "none":
        return "none"
    if gf.value == UNKNOWN:
        return UNKNOWN
    raw = " ".join((gf.quote or gf.note or str(gf.value)).split())
    if len(raw) <= 140:
        return raw
    cut = raw[:140].rsplit(" ", 1)[0].rstrip(",;:-")
    return (cut or raw[:137]) + "..."


def _apply_golden_correction(rec: AppRecord, field: str, gf, labeller: str, before_outcome: str) -> dict[str, Any]:
    """Mutate ``rec`` in place with a human-confirmed golden label value and return the diff entry."""
    if field == "main_blocker_type":
        claim = rec.claims.main_blocker
        before_val: Any = claim.value.model_dump()
        claim.value = Blocker(text=_clean_blocker_text(gf), type=gf.value)
        after_val: Any = claim.value.model_dump()
    else:
        claim = getattr(rec.claims, field)
        before_val = claim.value
        claim.value = gf.value
        after_val = gf.value

    if gf.url and gf.url.startswith(("http://", "https://")) and gf.quote:
        tier = "official_github" if "github.com/" in gf.url.lower() else "official_docs"
        claim.evidence = [Evidence(url=gf.url, quote=gf.quote, source_tier=tier)]
    elif claim.is_unknown():
        claim.evidence = []

    claim.extractor_confidence = "high" if not claim.is_unknown() else "low"
    claim.verification.status = "supported" if not claim.is_unknown() else "insufficient_evidence"
    note_add = f"human correction from confirmed golden label ({labeller})"
    claim.verification.notes = "; ".join(x for x in (claim.verification.notes, note_add) if x)

    return {
        "app_id": rec.app_id,
        "app": rec.app,
        "field": field,
        "before_value": before_val,
        "after_value": after_val,
        "before_outcome": before_outcome,
        "url": gf.url,
        "quote": gf.quote,
        "note": gf.note,
        "labeller": labeller,
        "source": "golden_label",
    }


def build_final(
    pass2_dir: Optional[Path] = None,
    final_dir: Optional[Path] = None,
    golden_dir: Optional[Path] = None,
    corrections_out: Optional[Path] = None,
    apps: Optional[list[registry.App]] = None,
) -> dict[str, Any]:
    """Copy pass2 -> final, apply confirmed golden-set corrections, compute buildability + buckets."""
    src_dir = Path(pass2_dir) if pass2_dir else accuracy.DATA_DIR / "pass2"
    dst_dir = Path(final_dir) if final_dir else accuracy.DATA_DIR / "final"
    g_dir = Path(golden_dir) if golden_dir else G.GOLDEN_DIR
    c_out = Path(corrections_out) if corrections_out else accuracy.REPORTS_DIR / "corrections.json"

    app_list = apps if apps is not None else registry.load_apps()
    app_by_id = {a.id: a for a in app_list}
    labels = G.load_labels(confirmed_only=True, golden_dir=g_dir)

    src_files = sorted(src_dir.glob(accuracy.RECORD_GLOB))
    if not src_files:
        raise FileNotFoundError(f"no pass2 records found in {src_dir}")

    dst_dir.mkdir(parents=True, exist_ok=True)
    corrections: list[dict[str, Any]] = []
    corrected_apps: set[int] = set()
    buckets: Counter[str] = Counter()
    verdicts: Counter[str] = Counter()

    for src_path in src_files:
        rec = load_record(src_path).model_copy(deep=True)
        rec.pass_ = "final"
        label = labels.get(rec.app_id)
        if label is not None:
            for field in GOLDEN_FIELDS:
                gf = label.fields[field]
                if gf.status == "ambiguous":
                    continue
                agent_val, _ = accuracy.agent_value(rec, field)
                outcome, _, _ = accuracy.score_claim(field, gf, agent_val)
                if outcome != "correct":
                    corrections.append(
                        _apply_golden_correction(rec, field, gf, label.labeller, outcome)
                    )
                    corrected_apps.add(rec.app_id)

        rec.buildability = score_record(rec, app_by_id.get(rec.app_id))
        rec.record_confidence = R._compute_confidence(rec.claims)  # type: ignore[assignment]
        validated = AppRecord.model_validate(rec.model_dump(by_alias=True))
        dump_record(validated, dst_dir / src_path.name)
        buckets[validated.buildability.bucket] += 1
        verdicts[validated.buildability.verdict] += 1

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pass_from": "pass2",
        "pass_to": "final",
        "total_records": len(src_files),
        "corrected_apps": len(corrected_apps),
        "total_corrections": len(corrections),
        "corrections": corrections,
    }
    c_out.parent.mkdir(parents=True, exist_ok=True)
    c_out.write_text(json.dumps(payload, indent=2, default=str) + "\n")

    return {
        "total_records": len(src_files),
        "corrected_apps": len(corrected_apps),
        "total_corrections": len(corrections),
        "bucket_counts": dict(buckets),
        "verdict_counts": dict(verdicts),
        "final_dir": str(dst_dir),
        "corrections_path": str(c_out),
    }
