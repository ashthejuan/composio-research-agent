"""Per-loop statistics from the claim ledgers of a verified pass (phase 3, task 7)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agent import accuracy
from agent.schema import GOLDEN_FIELDS, AppRecord, Claim, load_record

LOOPS = ("L1", "L2", "L3", "L4", "L5", "L6")
FLAGGED_RESULTS: dict[str, frozenset[str]] = {
    **{k: frozenset({"fail"}) for k in ("L1", "L2", "L3", "L4")},
    "L5": frozenset({"partially_supported", "contradicted", "insufficient_evidence"}),
    "L6": frozenset(),
}
CHANGED_RESULTS = frozenset({"changed", "set_unknown"})
STATUSES = ("supported", "partially_supported", "contradicted", "insufficient_evidence", "pending")


def claim_for(record: AppRecord, field: str) -> Claim:
    """The claim behind a golden field (main_blocker_type lives on main_blocker)."""
    return getattr(record.claims, "main_blocker" if field == "main_blocker_type" else field)


def is_flagged(loop: str, result: str) -> bool:
    return result in FLAGGED_RESULTS.get(loop, frozenset())


def is_change(loop: str, result: str) -> bool:
    return loop == "L6" and result in CHANGED_RESULTS


def _blank() -> dict[str, Any]:
    return {"loops": {lp: {"claims_checked": 0, "flagged": 0, "values_changed": 0, "errors": 0}
                      for lp in LOOPS},
            "status_distribution": {s: 0 for s in STATUSES}, "claims": 0}


def _add(block: dict[str, Any], claim: Claim) -> None:
    block["claims"] += 1
    block["status_distribution"][claim.verification.status] = (
        block["status_distribution"].get(claim.verification.status, 0) + 1)
    seen: dict[str, dict[str, bool]] = {}
    for le in claim.verification.loops:
        s = seen.setdefault(le.loop, {"flag": False, "chg": False, "err": False})
        s["flag"] |= is_flagged(le.loop, le.result)
        s["chg"] |= is_change(le.loop, le.result)
        s["err"] |= le.result == "error"
    for lp, s in seen.items():  # counts are per claim, not per ledger entry
        d = block["loops"][lp]
        d["claims_checked"] += 1
        d["flagged"] += s["flag"]
        d["values_changed"] += s["chg"]
        d["errors"] += s["err"]


def compute(records: list[AppRecord], fields: tuple[str, ...] = GOLDEN_FIELDS) -> dict[str, Any]:
    """Stats over the evaluated (golden) fields; ``all_claims`` repeats it over every claim."""
    overall, all_claims = _blank(), _blank()
    per_field = {f: _blank() for f in fields}
    for rec in records:
        for f in fields:
            c = claim_for(rec, f)
            _add(per_field[f], c)
            _add(overall, c)
        for name in type(rec.claims).model_fields:
            _add(all_claims, getattr(rec.claims, name))
    return {"fields": list(fields), "apps": len(records), "overall": overall,
            "per_field": per_field, "all_claims": all_claims}


def load_dir(records_dir: Path) -> list[AppRecord]:
    return [load_record(p) for p in sorted(Path(records_dir).glob(accuracy.RECORD_GLOB))]


def report_path(label: str) -> Path:
    return accuracy.REPORTS_DIR / ("loop_stats.json" if label == "pass2" else f"loop_stats_{label}.json")


def run(pass_name: str = "pass2", records_dir: Optional[Path] = None, out: Optional[Path] = None) -> dict[str, Any]:
    rdir = Path(records_dir) if records_dir else accuracy.default_records_dir(pass_name)
    stats = {"pass": pass_name, "records_dir": str(rdir), **compute(load_dir(rdir))}
    out = Path(out) if out else report_path(pass_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, indent=2) + "\n")
    return stats
