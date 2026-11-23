"""Static case-study site & agent-consumable export generator (Phase 5, FR-11 / FR-12).

Renders templates/index.html.j2 -> site/index.html and writes site/apps.json, site/apps.csv,
and site/llms.txt. No backend, no runtime LLM, works offline from file://.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from agent import accuracy
from agent.schema import AppRecord, load_record

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
DEFAULT_SITE_DIR = ROOT / "site"
REPO_URL = "https://github.com/ashthejuan/composio-research-agent"
README_URL = f"{REPO_URL}#readme"


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _safe_json_for_script(obj: Any) -> str:
    """Serialize JSON safe for embedding inside <script type='application/json'>."""
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


def _classify_human_theme(reason: str, action: str) -> str:
    text = f"{reason} {action}".lower()
    if any(k in text for k in ("golden", "draft", "label", "correction", "ambiguous", "holdout")):
        return "Golden labelling & corrections"
    if any(k in text for k in ("composio", "key", "env", "fallback", "exa", "slug", "instant")):
        return "Tool & API auth setup"
    if any(k in text for k in ("verifier", "model", "gemini", "muse", "gpt-")):
        return "Model & verifier configuration"
    if any(k in text for k in ("domain", "github", "rebrand", "allowlist", "registry", "catalog")):
        return "Registry & domain curation"
    return "Pipeline & runtime fixes"


def parse_human_log(path: Path) -> dict[str, Any]:
    """Parse data/human_log.md markdown table into structured entries and theme counts."""
    if not path.exists():
        return {"entries": [], "total": 0, "by_theme": []}
    entries: list[dict[str, str]] = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s.startswith("|") or not s.endswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].lower() == "when" or set(cells[0].replace("-", "").strip()) == set():
            continue
        when, app, reason, action = cells[0], cells[1], cells[2], cells[3]
        theme = _classify_human_theme(reason, action)
        entries.append({"when": when, "app": app, "reason": reason, "action": action, "theme": theme})
    counts = Counter(e["theme"] for e in entries)
    by_theme = [{"theme": t, "count": c} for t, c in counts.most_common()]
    return {"entries": entries, "total": len(entries), "by_theme": by_theme}


def _parse_changes_md(path: Path) -> list[dict[str, str]]:
    """Extract bulleted disclosure items from data/reports/changes.md."""
    if not path.exists():
        return []
    items: list[dict[str, str]] = []
    section = "General"
    for line in path.read_text().splitlines():
        s = line.strip()
        if s.startswith("## "):
            section = s[3:].strip()
        elif s.startswith("- "):
            items.append({"section": section, "text": s[2:].strip()})
    return items


def _write_csv(records: list[AppRecord], out_path: Path) -> None:
    cols = [
        "app_id", "app", "category", "score", "verdict", "bucket", "commercial_gate",
        "composio_toolkit", "composio_slug", "auth_methods", "credential_access",
        "production_gate", "api_type", "api_breadth", "mcp_official", "mcp_third_party",
        "free_tier_or_sandbox", "webhooks", "official_sdks", "main_blocker_type",
        "main_blocker_text", "docs_url", "record_confidence",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in records:
            c = r.claims
            w.writerow({
                "app_id": r.app_id,
                "app": r.app,
                "category": c.category.value,
                "score": r.buildability.score,
                "verdict": r.buildability.verdict,
                "bucket": r.buildability.bucket,
                "commercial_gate": r.buildability.commercial_gate,
                "composio_toolkit": r.composio_toolkit.value,
                "composio_slug": r.composio_toolkit.slug or "",
                "auth_methods": ";".join(c.auth_methods.value),
                "credential_access": c.credential_access.value,
                "production_gate": c.production_gate.value,
                "api_type": ";".join(c.api_type.value),
                "api_breadth": c.api_breadth.value,
                "mcp_official": c.mcp_official.value,
                "mcp_third_party": c.mcp_third_party.value,
                "free_tier_or_sandbox": c.free_tier_or_sandbox.value,
                "webhooks": c.webhooks.value,
                "official_sdks": ";".join(c.official_sdks.value),
                "main_blocker_type": c.main_blocker.value.type,
                "main_blocker_text": c.main_blocker.value.text,
                "docs_url": c.docs_url.value,
                "record_confidence": r.record_confidence,
            })


def _write_llms_txt(
    analytics: dict[str, Any],
    acc1: Optional[dict[str, Any]],
    acc2: Optional[dict[str, Any]],
    accf: Optional[dict[str, Any]],
    out_path: Path,
) -> None:
    s = analytics["summary"]
    b = analytics["distributions"]["bucket"]
    p1 = acc1["overall"]["accuracy"] if acc1 else "n/a"
    p2 = acc2["overall"]["accuracy"] if acc2 else "n/a"
    pf = accf["overall"]["accuracy"] if accf else "n/a"
    rules_hash = (acc2 or acc1 or {}).get("rules_sha256", "n/a")
    headlines = "\n".join(f"- {h['text']}" for h in analytics.get("headlines", []))
    holdout = analytics.get("holdout", {})
    text = f"""# App Intelligence Research Agent — Dataset & Case Study

> Evidence-backed integration & buildability audit of {s['total_apps']} applications across 10 categories for Composio.

## Files
- index.html: Interactive static case study with full dataset embedded in <script type="application/json" id="dataset">.
- apps.json: Complete claim-level JSON records for all {s['total_apps']} apps (final pass), including verbatim quotes, evidence URLs, verification loop ledgers (L1-L6), and deterministic buildability breakdowns.
- apps.csv: Flat one-row-per-app table with scores, verdicts, buckets, auth methods, access tiers, production gates, MCP status, Composio toolkit coverage, and blockers.
- llms.txt: This machine-readable overview.

## Schema Summary (apps.json)
Each item in apps.json is an AppRecord:
- app_id (1-100), app, pass ("final"), record_confidence ("high" | "medium" | "low")
- claims: 16 claim objects (category, description, auth_methods, credential_access, production_gate, api_type, api_breadth, mcp_official, mcp_third_party, free_tier_or_sandbox, webhooks, official_sdks, rate_limit_note, open_source_self_host, main_blocker, docs_url). Each claim carries value, evidence[] (url, quote, source_tier), extractor_confidence, and verification (status, loops[L1..L6], notes).
- composio_toolkit: value ("available" | "unavailable"), slug, source (deterministic catalog lookup, never LLM-guessed).
- buildability: score (0-10), verdict ("easy" | "moderate" | "difficult" | "blocked"), breakdown (5 anchored 0-2 dimensions with driver text), commercial_gate (bool), bucket ("easy_win" | "moderate" | "needs_outreach" | "technically_blocked").

## Opportunity Buckets (N={s['total_apps']})
- Easy wins: {b.get('easy_win', 0)} ({s['easy_wins_without_composio']} without a Composio toolkit)
- Moderate effort: {b.get('moderate', 0)}
- Needs outreach / BD (commercial gate): {b.get('needs_outreach', 0)}
- Technically blocked: {b.get('technically_blocked', 0)}

## Key Findings
{headlines}

## Accuracy & Verification Methodology
- Golden set: 20 stratified apps (2 per category, 120 evaluated claims across 6 fields), human-confirmed from primary documentation.
- Frozen scoring rules SHA-256: {rules_hash}
- First-pass automated accuracy (Pass 1, raw research agent): {p1}% (63/120 claims)
- Final automated verification accuracy (Pass 2, L1-L6 checks + independent Meta Muse Spark verifier): {p2}% (65/120 claims, +1.67 percentage points; introduced 3 regressions and filled 6 unknowns incorrectly)
- Post-review corrected-sample agreement (Final): {pf}% agreement with human labels on that same 120-claim sample after 55 human corrections (quality-control step, NOT independently measured model accuracy; independent post-correction accuracy was not measured)
- Holdout: {holdout.get('status', 'unknown')} — {holdout.get('note', '')}

## Repository
{REPO_URL}
"""
    out_path.write_text(text.strip() + "\n", encoding="utf-8")


def _compute_wrong_section(
    records: list[AppRecord],
    attribution: Optional[dict[str, Any]],
    corrections: Optional[dict[str, Any]],
    taxonomy: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble honest 'Where I was wrong' data from frozen reports and final records."""
    regressions = (attribution or {}).get("regressions", [])
    abstained_to_wrong = [
        c for c in (attribution or {}).get("claims", [])
        if c.get("transition") == "abstained_to_wrong"
    ]
    # Apps with most remaining unknowns across the 6 evaluated fields + low confidence
    hard_apps = []
    for r in records:
        eval_unknowns = sum(
            1
            for f in ("auth_methods", "credential_access", "production_gate", "api_type", "mcp_official", "main_blocker")
            if getattr(r.claims, f).is_unknown()
        )
        if eval_unknowns >= 3 or r.record_confidence == "low" or r.buildability.bucket == "technically_blocked":
            hard_apps.append({
                "app_id": r.app_id,
                "app": r.app,
                "category": r.claims.category.value,
                "confidence": r.record_confidence,
                "bucket": r.buildability.bucket,
                "score": r.buildability.score,
                "eval_unknowns": eval_unknowns,
                "blocker_text": r.claims.main_blocker.value.text,
            })
    hard_apps.sort(key=lambda x: (-x["eval_unknowns"], x["score"], x["app_id"]))

    # Most abstained fields across all 100 apps in final
    unknown_by_field = []
    for fname in ("production_gate", "credential_access", "main_blocker", "mcp_official", "api_type", "auth_methods", "docs_url"):
        ct = sum(1 for r in records if getattr(r.claims, fname).is_unknown())
        unknown_by_field.append({"field": fname, "unknown_count": ct})
    unknown_by_field.sort(key=lambda x: -x["unknown_count"])

    return {
        "regressions": regressions,
        "abstained_to_wrong": abstained_to_wrong,
        "hard_apps": hard_apps[:12],
        "unknown_by_field": unknown_by_field,
        "total_corrections": (corrections or {}).get("total_corrections", 0),
        "corrected_apps": (corrections or {}).get("corrected_apps", 0),
        "taxonomy": (taxonomy or {}).get("summary"),
    }


def build_site(
    *,
    final_dir: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
    human_log_path: Optional[Path] = None,
    templates_dir: Optional[Path] = None,
    out_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Generate site/index.html, site/apps.json, site/apps.csv, and site/llms.txt."""
    f_dir = Path(final_dir) if final_dir else accuracy.DATA_DIR / "final"
    r_dir = Path(reports_dir) if reports_dir else accuracy.REPORTS_DIR
    h_path = Path(human_log_path) if human_log_path else accuracy.DATA_DIR / "human_log.md"
    t_dir = Path(templates_dir) if templates_dir else TEMPLATES_DIR
    s_dir = Path(out_dir) if out_dir else DEFAULT_SITE_DIR

    files = sorted(f_dir.glob(accuracy.RECORD_GLOB))
    if not files:
        raise FileNotFoundError(f"no final records found in {f_dir}")
    records = [load_record(p) for p in files]
    records_dicts = [r.model_dump(mode="json", by_alias=True) for r in records]

    analytics = _load_json(r_dir / "analytics.json")
    if analytics is None:
        from agent import analytics as analytics_mod

        analytics = analytics_mod.compute_analytics(records, reports_dir=r_dir)

    acc1 = _load_json(r_dir / "accuracy_pass1.json")
    acc2 = _load_json(r_dir / "accuracy_pass2.json")
    accf = _load_json(r_dir / "accuracy_final.json")
    loop_stats = _load_json(r_dir / "loop_stats.json")
    attribution = _load_json(r_dir / "attribution.json")
    corrections = _load_json(r_dir / "corrections.json")
    taxonomy = _load_json(r_dir / "taxonomy_pass1.json")
    mcp_audit = _load_json(r_dir / "mcp_audit.json")
    run1 = _load_json(r_dir / "run_pass1.json")
    run2 = _load_json(r_dir / "run_pass2.json")
    changes = _parse_changes_md(r_dir / "changes.md")
    human_log = parse_human_log(h_path)
    wrong_data = _compute_wrong_section(records, attribution, corrections, taxonomy)

    dataset = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo_url": REPO_URL,
        "readme_url": README_URL,
        "records": records_dicts,
        "analytics": analytics,
        "accuracy": {"pass1": acc1, "pass2": acc2, "final": accf},
        "loop_stats": loop_stats,
        "attribution": attribution,
        "corrections": corrections,
        "taxonomy": taxonomy,
        "mcp_audit": mcp_audit,
        "changes": changes,
        "human_log": human_log,
        "runs": {"pass1": run1, "pass2": run2},
        "wrong": wrong_data,
    }

    s_dir.mkdir(parents=True, exist_ok=True)
    (s_dir / "apps.json").write_text(json.dumps(records_dicts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_csv(records, s_dir / "apps.csv")
    _write_llms_txt(analytics, acc1, acc2, accf, s_dir / "llms.txt")

    env = Environment(
        loader=FileSystemLoader(str(t_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    tpl = env.get_template("index.html.j2")
    html = tpl.render(
        dataset_json=_safe_json_for_script(dataset),
        analytics=analytics,
        acc1=acc1,
        acc2=acc2,
        accf=accf,
        loop_stats=loop_stats,
        attribution=attribution,
        corrections=corrections,
        mcp_audit=mcp_audit,
        changes=changes,
        human_log=human_log,
        run1=run1,
        run2=run2,
        wrong=wrong_data,
        repo_url=REPO_URL,
        readme_url=README_URL,
    )
    index_path = s_dir / "index.html"
    index_path.write_text(html, encoding="utf-8")

    return {
        "out_dir": str(s_dir),
        "index_html": str(index_path),
        "apps_json": str(s_dir / "apps.json"),
        "apps_csv": str(s_dir / "apps.csv"),
        "llms_txt": str(s_dir / "llms.txt"),
        "records": len(records),
        "html_bytes": index_path.stat().st_size,
    }
