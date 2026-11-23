"""Accuracy scorer implementing data/golden/_rules.md (FR-08) exactly.

Outcomes per claim: correct / wrong / abstained / excluded; ``partial`` is a
sub-count of wrong (set fields that overlap the golden set without equalling it).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from agent import golden as G
from agent.schema import GOLDEN_FIELDS, AppRecord, GoldenField, load_record

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = DATA_DIR / "reports"
RULES_PATH = G.GOLDEN_DIR / "_rules.md"
SET_FIELDS = G.SET_FIELDS
UNKNOWN = G.UNKNOWN
PROVISIONAL_MARK = "PROVISIONAL"
RECORD_GLOB = "[0-9][0-9][0-9]-*.json"  # never matches _frozen.json / run_*.json
_AUTH_CLASS = {"bearer_token": "api_key"}  # both are static secrets


def rules_sha256(path: Path = RULES_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> Optional[str]:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                           timeout=10, cwd=ROOT)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _as_set(v: Any) -> frozenset[str]:
    if isinstance(v, (list, tuple, set, frozenset)):
        return frozenset(str(x) for x in v)
    return frozenset([str(v)])


def _auth_classes(s: frozenset[str]) -> frozenset[str]:
    return frozenset(_AUTH_CLASS.get(x, x) for x in s)


def _is_unknown(s: frozenset[str]) -> bool:
    return s == frozenset([UNKNOWN])


def agent_value(record: AppRecord, field: str) -> tuple[Any, list[str]]:
    """Return (raw value, evidence urls) for a golden field."""
    if field == "main_blocker_type":
        c = record.claims.main_blocker
        return c.value.type, [e.url for e in c.evidence]
    c = getattr(record.claims, field)
    return c.value, [e.url for e in c.evidence]


def score_claim(field: str, golden: GoldenField, agent: Any) -> tuple[str, bool, bool]:
    """Return (outcome, partial, strict_correct) for one claim.

    outcome in {correct, wrong, abstained, excluded}.
    """
    if golden.status == "ambiguous":
        return "excluded", False, False
    g, a = _as_set(golden.value), _as_set(agent)
    g_unk, a_unk = _is_unknown(g), _is_unknown(a)
    if g_unk:  # human confirmed docs do not establish it
        ok = a_unk
        return ("correct" if ok else "wrong"), False, ok
    if a_unk:
        return "abstained", False, False
    if field == "auth_methods":
        gc, ac = _auth_classes(g), _auth_classes(a)
        strict = g == a
        if gc == ac:
            return "correct", False, strict
        return "wrong", bool(gc & ac), strict
    if g == a:
        return "correct", False, True
    return "wrong", field in SET_FIELDS and bool(g & a), False


def _counts(rows: list[dict]) -> dict[str, Any]:
    ex = sum(r["outcome"] == "excluded" for r in rows)
    ev = len(rows) - ex
    n = {k: sum(r["outcome"] == k for r in rows) for k in ("correct", "wrong", "abstained")}
    partial = sum(bool(r["partial"]) for r in rows if r["outcome"] == "wrong")

    def pct(x: int) -> Optional[float]:
        return round(100 * x / ev, 2) if ev else None

    return {"claims": len(rows), "evaluated": ev, "excluded": ex, **n, "partial": partial,
            "accuracy": pct(n["correct"]), "wrong_rate": pct(n["wrong"]),
            "abstain_rate": pct(n["abstained"])}


class AccuracyReport(BaseModel):
    rules_sha256: str
    pass_name: str = Field(serialization_alias="pass")
    scored_at: str
    git_commit: Optional[str] = None
    provisional: bool = False
    incomplete: bool = False
    n_apps: int = 0
    n_labels_missing: int = 0
    labels_missing: list[int] = Field(default_factory=list)
    records_missing: list[int] = Field(default_factory=list)
    overall: dict[str, Any] = Field(default_factory=dict)
    strict_auth_accuracy: Optional[float] = None
    per_field: dict[str, dict[str, Any]] = Field(default_factory=dict)
    per_app: dict[str, dict[str, Any]] = Field(default_factory=dict)
    misses: list[dict[str, Any]] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    claim_outcomes: list[dict[str, Any]] = Field(default_factory=list)  # every scored claim (additive)
    taxonomy: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(by_alias=True), indent=2) + "\n"


def default_records_dir(pass_name: str) -> Path:
    frozen = DATA_DIR / pass_name
    if frozen.is_dir() and any(frozen.glob(RECORD_GLOB)):
        return frozen
    return DATA_DIR / "work" / pass_name


class AmbiguousRecord(RuntimeError):
    """More than one record file matches an app id."""


def _find_record(records_dir: Path, app_id: int) -> Optional[Path]:
    """The single ``<id:03d>-<slug>.json`` record for ``app_id`` (None if absent).

    Raises AmbiguousRecord when several files match, instead of silently
    scoring whichever sorts first.
    """
    hits = sorted(p for p in records_dir.glob(f"{app_id:03d}-*.json") if p.is_file())
    if len(hits) > 1:
        raise AmbiguousRecord(f"app {app_id}: several records {[h.name for h in hits]}")
    return hits[0] if hits else None


def official_report_path(pass_name: str) -> Path:
    return REPORTS_DIR / f"accuracy_{pass_name}.json"


def provisional_report_path(pass_name: str) -> Path:
    return REPORTS_DIR / f"accuracy_{pass_name}.{PROVISIONAL_MARK}_drafts.json"


def write_report(report: "AccuracyReport", path: Path) -> Path:
    """Write ``report``; a provisional report must carry PROVISIONAL in its name.

    This is the guard that keeps draft-scored numbers off every official
    ``accuracy_<pass>.json`` path, whatever ``--out`` or pass name is given.
    """
    path = Path(path)
    if report.provisional and PROVISIONAL_MARK not in path.name:
        raise ValueError(f"provisional report must not be written to {path} "
                         f"(file name must contain {PROVISIONAL_MARK!r})")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())
    return path


def score(
    pass_name: str,
    ids: tuple[int, ...] | list[int] = G.GOLDEN_IDS,
    label_dir: Optional[Path] = None,
    records_dir: Optional[Path] = None,
    include_drafts: bool = False,
    rules_path: Path = RULES_PATH,
) -> AccuracyReport:
    """Score ``pass_name`` records against golden labels.

    ``label_dir`` is the golden directory (confirmed labels at its top level,
    drafts in ``drafts/``). With ``include_drafts`` the labels come from drafts
    (falling back to confirmed where no draft exists) and the report is PROVISIONAL.
    """
    rdir = Path(records_dir) if records_dir else default_records_dir(pass_name)
    labels = G.load_labels(confirmed_only=True, ids=ids, golden_dir=label_dir)
    source = {i: "confirmed" for i in labels}
    if include_drafts:
        drafts = G.load_labels(confirmed_only=False, ids=ids, golden_dir=label_dir)
        labels = {**labels, **drafts}
        source.update({i: "draft" for i in drafts})
    missing_labels = [i for i in ids if i not in labels]
    missing_records: list[int] = []
    rows: list[dict] = []
    per_app: dict[str, dict] = {}
    misses: list[dict] = []
    excluded: list[dict] = []
    claim_outcomes: list[dict] = []
    strict_rows: list[bool] = []

    for i in ids:
        if i not in labels:
            continue
        path = _find_record(rdir, i)
        if path is None:
            missing_records.append(i)
            continue
        rec, label = load_record(path), labels[i]
        if rec.app_id != i:
            raise ValueError(f"{path.name}: record app_id {rec.app_id} != {i}")
        app_rows = []
        for f in GOLDEN_FIELDS:
            gf = label.fields[f]
            val, urls = agent_value(rec, f)
            outcome, partial, strict = score_claim(f, gf, val)
            row = {"app_id": i, "field": f, "outcome": outcome, "partial": partial}
            rows.append(row)
            app_rows.append(row)
            if f == "auth_methods" and outcome != "excluded":
                strict_rows.append(strict)
            info = {"app_id": i, "app": rec.app, "field": f, "golden": gf.value,
                    "agent": val, "golden_url": gf.url, "agent_evidence_urls": urls}
            claim_outcomes.append({
                "app_id": i, "app": rec.app, "field": f,
                "outcome": "partial" if (outcome == "wrong" and partial) else outcome,
                "golden": gf.value, "agent": val})
            if outcome == "excluded":
                excluded.append({**info, "outcome": outcome, "note": gf.note})
            elif outcome != "correct":
                misses.append({**info, "outcome": "partial" if partial else outcome,
                               "tag": None})
        per_app[str(i)] = {"app": rec.app, "label_source": source[i], **_counts(app_rows),
                           "fields": {r["field"]: r["outcome"] for r in app_rows}}

    return AccuracyReport(
        rules_sha256=rules_sha256(rules_path), pass_name=pass_name,
        scored_at=datetime.now(timezone.utc).isoformat(), git_commit=_git_commit(),
        provisional=include_drafts,
        incomplete=bool(missing_labels or missing_records),
        n_apps=len(per_app), n_labels_missing=len(missing_labels),
        labels_missing=missing_labels, records_missing=missing_records,
        overall=_counts(rows),
        strict_auth_accuracy=(round(100 * sum(strict_rows) / len(strict_rows), 2)
                              if strict_rows else None),
        per_field={f: _counts([r for r in rows if r["field"] == f]) for f in GOLDEN_FIELDS},
        per_app=per_app, misses=misses, excluded=excluded,
        claim_outcomes=claim_outcomes, taxonomy={},
    )
