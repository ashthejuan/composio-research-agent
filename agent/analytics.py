"""Cross-app analytics, opportunity lists, templated headlines, and sanity checks (FR-09 / FR-10)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, get_args

from agent import accuracy
from agent import golden as G
from agent.schema import UNKNOWN, AppRecord, Category, load_record

CATEGORIES: tuple[str, ...] = get_args(Category)

PRODUCTION_GATED_VALUES = frozenset({"oauth_app_review", "marketplace_listing", "partner_program"})
COMMERCIAL_ACCESS = frozenset({"partner_gated", "contact_sales"})
VALID_BUCKETS = ("easy_win", "moderate", "needs_outreach", "technically_blocked")


def _app_summary(rec: AppRecord) -> dict[str, Any]:
    return {
        "app_id": rec.app_id,
        "app": rec.app,
        "category": rec.claims.category.value,
        "score": rec.buildability.score,
        "verdict": rec.buildability.verdict,
        "bucket": rec.buildability.bucket,
        "commercial_gate": rec.buildability.commercial_gate,
        "composio_toolkit": rec.composio_toolkit.value,
        "composio_slug": rec.composio_toolkit.slug,
        "credential_access": rec.claims.credential_access.value,
        "production_gate": rec.claims.production_gate.value,
        "auth_methods": list(rec.claims.auth_methods.value),
        "api_type": list(rec.claims.api_type.value),
        "mcp_official": rec.claims.mcp_official.value,
        "mcp_third_party": rec.claims.mcp_third_party.value,
        "main_blocker_type": rec.claims.main_blocker.value.type,
        "main_blocker_text": rec.claims.main_blocker.value.text,
    }


def _expected_verdict(score: int) -> str:
    if score >= 8:
        return "easy"
    if score >= 5:
        return "moderate"
    if score >= 3:
        return "difficult"
    return "blocked"


def _expected_bucket(score: int, commercial_gate: bool) -> str:
    if commercial_gate:
        return "needs_outreach"
    if score >= 8:
        return "easy_win"
    if score >= 5:
        return "moderate"
    return "technically_blocked"


def _run_sanity_checks(records: list[AppRecord], opportunities: dict[str, list[dict]]) -> dict[str, Any]:
    bucket_counts = Counter(r.buildability.bucket for r in records)
    seen_buckets: dict[int, list[str]] = defaultdict(list)
    for bkey, opp_key in (
        ("easy_win", "easy_wins_all"),
        ("needs_outreach", "outreach_targets"),
        ("moderate", "moderate"),
        ("technically_blocked", "technically_blocked"),
    ):
        for item in opportunities[opp_key]:
            seen_buckets[item["app_id"]].append(bkey)

    duplicate_bucket_apps = [aid for aid, bs in seen_buckets.items() if len(bs) != 1]
    breakdown_mismatches: list[int] = []
    verdict_mismatches: list[int] = []
    bucket_precedence_mismatches: list[int] = []

    for r in records:
        b = r.buildability
        if b.breakdown is None or b.score != b.breakdown.total():
            breakdown_mismatches.append(r.app_id)
        if b.verdict != _expected_verdict(b.score):
            verdict_mismatches.append(r.app_id)
        expected_gate = (
            r.claims.credential_access.value in COMMERCIAL_ACCESS
            or r.claims.production_gate.value == "partner_program"
        )
        if b.commercial_gate != expected_gate or b.bucket != _expected_bucket(b.score, expected_gate):
            bucket_precedence_mismatches.append(r.app_id)

    unknown_buckets = bucket_counts.get("unknown", 0)
    bucket_sum = sum(bucket_counts.get(k, 0) for k in VALID_BUCKETS)
    all_passed = (
        bucket_sum == len(records)
        and unknown_buckets == 0
        and not duplicate_bucket_apps
        and not breakdown_mismatches
        and not verdict_mismatches
        and not bucket_precedence_mismatches
    )
    return {
        "total_apps": len(records),
        "bucket_sum": bucket_sum,
        "unknown_buckets": unknown_buckets,
        "duplicate_bucket_apps": duplicate_bucket_apps,
        "breakdown_mismatches": breakdown_mismatches,
        "verdict_mismatches": verdict_mismatches,
        "bucket_precedence_mismatches": bucket_precedence_mismatches,
        "all_passed": all_passed,
    }


def _build_headlines(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Build plain-English headlines dynamically from computed analytics (zero hardcoded numbers)."""
    n = data["summary"]["total_apps"]
    auth = data["distributions"]["auth_methods"]
    oauth2 = auth["by_method"].get("oauth2", 0)
    oauth2_gated = data["distributions"]["production_gate"]["oauth2_with_production_gate"]
    free_dev = data["summary"]["self_serve_free"]
    free_but_gated = data["distributions"]["production_gate"]["gated_despite_free_dev"]
    mcp_off = data["summary"]["official_mcp_yes"]
    mcp_no_comp = len(data["mcp_vs_composio"]["official_mcp_without_composio"])
    easy_total = data["summary"]["easy_wins"]
    easy_no_comp = data["summary"]["easy_wins_without_composio"]
    outreach_total = data["distributions"]["bucket"].get("needs_outreach", 0)
    outreach_no_comp = len(data["opportunities"]["outreach_without_composio"])

    # Top outreach category
    cat_buckets = data["matrices"]["category_x_bucket"]
    top_outreach_cat, top_outreach_n = max(
        ((cat, row.get("needs_outreach", 0)) for cat, row in cat_buckets.items()),
        key=lambda kv: (kv[1], kv[0]),
        default=("Unknown", 0),
    )

    # Top concrete blocker type (excluding unknown/none if any concrete blocker exists)
    concrete_blockers = [b for b in data["blockers"]["by_type"] if b["type"] not in (UNKNOWN, "none")]
    top_blocker = concrete_blockers[0] if concrete_blockers else {"type": "unknown", "count": 0, "pct": 0.0}

    rest_n = data["distributions"]["api_type"].get("rest", 0)
    multi_auth = auth["multi_method_apps"]

    specs = [
        (
            "free_dev_vs_production_gates",
            "Free developer access hides production gates",
            "{free_dev} of {n} apps offer free self-serve developer access, yet {free_but_gated} of those still require an app review, marketplace listing, or partner program before serving multi-tenant customers.",
            {"free_dev": free_dev, "n": n, "free_but_gated": free_but_gated},
        ),
        (
            "mcp_adoption_vs_composio",
            "Official MCP servers are mainstream — and reveal coverage gaps",
            "{mcp_off} of {n} apps already publish an official MCP server, including {mcp_no_comp} apps that do not yet have a Composio toolkit.",
            {"mcp_off": mcp_off, "n": n, "mcp_no_comp": mcp_no_comp},
        ),
        (
            "easy_wins_gap",
            "Immediate easy-win build opportunities without Composio coverage",
            "{easy_total} of {n} apps score as Easy Wins (8–10/10 with no commercial gate); {easy_no_comp} of them currently lack a Composio toolkit and can be built without BD outreach.",
            {"easy_total": easy_total, "n": n, "easy_no_comp": easy_no_comp},
        ),
        (
            "outreach_and_commercial_gates",
            "Commercial gates force BD outreach regardless of API quality",
            "{outreach_total} of {n} apps trigger a commercial gate (partner-gated, contact-sales, or partner program) — {outreach_no_comp} without Composio coverage — led by {top_outreach_cat} ({top_outreach_n} apps).",
            {
                "outreach_total": outreach_total,
                "n": n,
                "outreach_no_comp": outreach_no_comp,
                "top_outreach_cat": top_outreach_cat,
                "top_outreach_n": top_outreach_n,
            },
        ),
        (
            "dominant_blocker",
            "Top integration blocker across the catalog",
            "'{blocker_type}' is the most common integration blocker across the catalog ({blocker_count} of {n} apps, {blocker_pct:.0f}%).",
            {
                "blocker_type": top_blocker["type"],
                "blocker_count": top_blocker["count"],
                "n": n,
                "blocker_pct": top_blocker["pct"],
            },
        ),
        (
            "oauth_and_multi_auth",
            "REST + OAuth2 dominate, with multi-method auth common",
            "{rest_n} of {n} apps expose REST APIs and {oauth2} support OAuth2 ({oauth2_gated} of those with documented production gates), while {multi_auth} apps support two or more authentication methods.",
            {
                "rest_n": rest_n,
                "n": n,
                "oauth2": oauth2,
                "oauth2_gated": oauth2_gated,
                "multi_auth": multi_auth,
            },
        ),
    ]
    return [
        {"id": hid, "title": title, "text": tpl.format(**vals), "template": tpl, "values": vals}
        for hid, title, tpl, vals in specs
    ]


def compute_analytics(
    records: list[AppRecord],
    reports_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Compute all FR-09 analytics, FR-10 opportunity lists, headlines, and sanity checks."""
    rdir = Path(reports_dir) if reports_dir else accuracy.REPORTS_DIR
    n = len(records)

    # Distributions
    auth_by_method: Counter[str] = Counter()
    multi_method_apps = single_method_apps = no_auth_apps = unknown_auth_apps = 0
    for r in records:
        vals = list(r.claims.auth_methods.value)
        for v in vals:
            auth_by_method[v] += 1
        concrete = [v for v in vals if v not in (UNKNOWN, "none")]
        if len(concrete) >= 2:
            multi_method_apps += 1
        elif len(concrete) == 1:
            single_method_apps += 1
        elif vals == ["none"]:
            no_auth_apps += 1
        else:
            unknown_auth_apps += 1

    cred_dist = Counter(r.claims.credential_access.value for r in records)
    gate_dist = Counter(r.claims.production_gate.value for r in records)
    gated_despite_free_dev = sum(
        1
        for r in records
        if r.claims.credential_access.value == "self_serve_free"
        and r.claims.production_gate.value in PRODUCTION_GATED_VALUES
    )
    oauth2_with_production_gate = sum(
        1
        for r in records
        if "oauth2" in r.claims.auth_methods.value
        and r.claims.production_gate.value in PRODUCTION_GATED_VALUES
    )

    api_type_dist: Counter[str] = Counter()
    for r in records:
        for t in r.claims.api_type.value:
            api_type_dist[t] += 1

    breadth_dist = Counter(r.claims.api_breadth.value for r in records)
    mcp_off_dist = Counter(r.claims.mcp_official.value for r in records)
    mcp_3p_dist = Counter(r.claims.mcp_third_party.value for r in records)
    comp_dist = Counter(r.composio_toolkit.value for r in records)
    verdict_dist = Counter(r.buildability.verdict for r in records)
    bucket_dist = Counter(r.buildability.bucket for r in records)
    free_tier_dist = Counter(r.claims.free_tier_or_sandbox.value for r in records)
    webhooks_dist = Counter(r.claims.webhooks.value for r in records)
    ver_status_dist: Counter[str] = Counter()
    for r in records:
        for fname in type(r.claims).model_fields:
            ver_status_dist[getattr(r.claims, fname).verification.status] += 1

    # Matrices (canonical category order)
    cat_access: dict[str, dict[str, int]] = {c: {} for c in CATEGORIES}
    cat_auth: dict[str, dict[str, int]] = {c: {} for c in CATEGORIES}
    cat_bucket: dict[str, dict[str, int]] = {c: {} for c in CATEGORIES}
    for r in records:
        cat = str(r.claims.category.value)
        cat_access.setdefault(cat, {})
        cat_auth.setdefault(cat, {})
        cat_bucket.setdefault(cat, {})
        acc = r.claims.credential_access.value
        cat_access[cat][acc] = cat_access[cat].get(acc, 0) + 1
        for m in r.claims.auth_methods.value:
            cat_auth[cat][m] = cat_auth[cat].get(m, 0) + 1
        bk = r.buildability.bucket
        cat_bucket[cat][bk] = cat_bucket[cat].get(bk, 0) + 1

    # Blockers
    blocker_counts = Counter(r.claims.main_blocker.value.type for r in records)
    blockers_by_type = [
        {"type": bt, "count": ct, "pct": round(100.0 * ct / n, 1) if n else 0.0}
        for bt, ct in blocker_counts.most_common()
    ]
    examples_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in sorted(records, key=lambda x: x.app_id):
        bt = r.claims.main_blocker.value.type
        if len(examples_by_type[bt]) < 10:
            examples_by_type[bt].append(
                {"app_id": r.app_id, "app": r.app, "text": r.claims.main_blocker.value.text}
            )

    # MCP vs Composio
    mcp_off_x_comp: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    mcp_3p_x_comp: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    official_mcp_without_composio: list[dict[str, Any]] = []
    composio_without_official_mcp = 0
    for r in sorted(records, key=lambda x: x.app_id):
        mo = r.claims.mcp_official.value
        m3 = r.claims.mcp_third_party.value
        ct = r.composio_toolkit.value
        mcp_off_x_comp[mo][ct] += 1
        mcp_3p_x_comp[m3][ct] += 1
        if mo == "yes" and ct != "available":
            official_mcp_without_composio.append(_app_summary(r))
        if ct == "available" and mo != "yes":
            composio_without_official_mcp += 1

    # Opportunities (FR-10)
    summaries = [_app_summary(r) for r in records]
    easy_wins_all = sorted(
        [s for s in summaries if s["bucket"] == "easy_win"],
        key=lambda s: (s["composio_toolkit"] == "available", -s["score"], s["app_id"]),
    )
    easy_wins_without_composio = [s for s in easy_wins_all if s["composio_toolkit"] != "available"]
    outreach_targets = sorted(
        [s for s in summaries if s["bucket"] == "needs_outreach"],
        key=lambda s: (s["composio_toolkit"] == "available", -s["score"], s["app_id"]),
    )
    outreach_without_composio = [s for s in outreach_targets if s["composio_toolkit"] != "available"]
    moderate = sorted(
        [s for s in summaries if s["bucket"] == "moderate"],
        key=lambda s: (-s["score"], s["app_id"]),
    )
    technically_blocked = sorted(
        [s for s in summaries if s["bucket"] == "technically_blocked"],
        key=lambda s: (s["score"], s["app_id"]),
    )
    opportunities = {
        "easy_wins_without_composio": easy_wins_without_composio,
        "easy_wins_all": easy_wins_all,
        "outreach_targets": outreach_targets,
        "outreach_without_composio": outreach_without_composio,
        "moderate": moderate,
        "technically_blocked": technically_blocked,
    }

    # Holdout status
    holdout_path = rdir / "accuracy_holdout.json"
    if holdout_path.exists():
        holdout: dict[str, Any] = {
            "status": "completed",
            "report": json.loads(holdout_path.read_text()),
        }
    else:
        by_id = {r.app_id: r.app for r in records}
        names = [by_id[i] for i in G.HOLDOUT_IDS if i in by_id]
        names_str = f" ({', '.join(names)})" if names else ""
        holdout = {
            "status": "skipped_per_prd_cut_line_1",
            "holdout_ids": list(G.HOLDOUT_IDS),
            "note": (
                f"{len(G.HOLDOUT_IDS)} holdout apps{names_str} were not "
                "human-labelled per PRD §12 cut line #1; this is disclosed on the case-study page."
            ),
        }

    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_apps": n,
            "scored_apps": sum(1 for r in records if r.buildability.breakdown is not None),
            "commercial_gated": sum(1 for r in records if r.buildability.commercial_gate),
            "easy_wins": len(easy_wins_all),
            "easy_wins_without_composio": len(easy_wins_without_composio),
            "composio_available": comp_dist.get("available", 0),
            "composio_unavailable": comp_dist.get("unavailable", 0),
            "official_mcp_yes": mcp_off_dist.get("yes", 0),
            "self_serve_free": cred_dist.get("self_serve_free", 0),
            "oauth2_supported": auth_by_method.get("oauth2", 0),
        },
        "distributions": {
            "auth_methods": {
                "by_method": dict(auth_by_method),
                "multi_method_apps": multi_method_apps,
                "single_method_apps": single_method_apps,
                "no_auth_apps": no_auth_apps,
                "unknown_apps": unknown_auth_apps,
            },
            "credential_access": dict(cred_dist),
            "production_gate": {
                "by_value": dict(gate_dist),
                "gated_despite_free_dev": gated_despite_free_dev,
                "oauth2_with_production_gate": oauth2_with_production_gate,
            },
            "api_type": dict(api_type_dist),
            "api_breadth": dict(breadth_dist),
            "mcp_official": dict(mcp_off_dist),
            "mcp_third_party": dict(mcp_3p_dist),
            "composio_toolkit": dict(comp_dist),
            "verdict": dict(verdict_dist),
            "bucket": dict(bucket_dist),
            "free_tier_or_sandbox": dict(free_tier_dist),
            "webhooks": dict(webhooks_dist),
            "verification_status": dict(ver_status_dist),
        },
        "matrices": {
            "category_x_credential_access": cat_access,
            "category_x_auth_methods": cat_auth,
            "category_x_bucket": cat_bucket,
        },
        "blockers": {
            "by_type": blockers_by_type,
            "examples_by_type": dict(examples_by_type),
        },
        "mcp_vs_composio": {
            "official_mcp_x_composio": {k: dict(v) for k, v in mcp_off_x_comp.items()},
            "third_party_mcp_x_composio": {k: dict(v) for k, v in mcp_3p_x_comp.items()},
            "official_mcp_without_composio": official_mcp_without_composio,
            "composio_without_official_mcp": composio_without_official_mcp,
        },
        "opportunities": opportunities,
        "sanity_checks": _run_sanity_checks(records, opportunities),
        "holdout": holdout,
    }
    result["headlines"] = _build_headlines(result)
    return result


def run(
    pass_name: str = "final",
    records_dir: Optional[Path] = None,
    out: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
) -> dict[str, Any]:
    rdir = Path(records_dir) if records_dir else accuracy.default_records_dir(pass_name)
    files = sorted(rdir.glob(accuracy.RECORD_GLOB))
    if not files:
        raise FileNotFoundError(f"no records found in {rdir}")
    records = [load_record(p) for p in files]
    res = compute_analytics(records, reports_dir=reports_dir)
    res["pass"] = pass_name
    res["records_dir"] = str(rdir)
    out_path = Path(out) if out else accuracy.REPORTS_DIR / "analytics.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(res, indent=2, default=str) + "\n")
    return res
