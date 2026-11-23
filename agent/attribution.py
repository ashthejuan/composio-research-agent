"""Attribute pass-1 -> pass-2 accuracy changes to verification loops (phase 3, task 10).

Joins two freshly computed AccuracyReports (same golden labels, same frozen rules hash) with the
after-pass ledgers. Nothing is invented: a claim without ledger entries has responsible_loops = [].
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from agent import accuracy
from agent import golden as G
from agent.loopstats import LOOPS, claim_for, is_change, is_flagged
from agent.schema import GOLDEN_FIELDS, load_record


class RulesMismatch(RuntimeError):
    pass


def _cls(outcome: str) -> str:
    return "wrong" if outcome in ("wrong", "partial") else outcome


def transition(before: str, after: str) -> str:
    b, a = _cls(before), _cls(after)
    if "excluded" in (b, a):
        return "excluded"
    if a == "correct":
        return "unchanged_correct" if b == "correct" else "fixed"
    if b == "correct":
        return "regressed"
    if b == a:
        return f"unchanged_{a}"
    return f"{b}_to_{a}"  # wrong_to_abstained / abstained_to_wrong


def responsible(ledger: list[dict]) -> tuple[list[str], Optional[str]]:
    """(responsible loops in ledger order, primary trigger) for one claim's ledger entries."""
    # Exclude candidate re-check entries emitted during L6 attempts so a rejected L6 candidate's L3/L5
    # failure is never misattributed as the primary trigger for an already-unknown claim.
    flagged = [(i, e) for i, e in enumerate(ledger)
               if is_flagged(e["loop"], e["result"]) and not str(e.get("detail", "")).startswith("[L6 attempt ")]
    changes = [(i, e) for i, e in enumerate(ledger) if is_change(e["loop"], e["result"])]
    if changes:
        first = changes[0][0]
        picked = [e["loop"] for i, e in flagged if i < first] + [e["loop"] for _, e in changes]
    else:
        picked = [e["loop"] for _, e in flagged]
    loops = [lp for lp in LOOPS if lp in set(picked)]
    primary = flagged[0][1]["loop"] if flagged else (changes[0][1]["loop"] if changes else None)
    return loops, primary


def build(before: accuracy.AccuracyReport, after: accuracy.AccuracyReport, after_dir: Path) -> dict[str, Any]:
    if before.rules_sha256 != after.rules_sha256:
        raise RulesMismatch(f"rules hash differs: {before.rules_sha256} vs {after.rules_sha256}")
    b_map = {(r["app_id"], r["field"]): r for r in before.claim_outcomes}
    a_map = {(r["app_id"], r["field"]): r for r in after.claim_outcomes}
    keys = sorted(set(b_map) & set(a_map))
    ledgers: dict[int, Any] = {}
    claims: list[dict] = []
    for app_id, field in keys:
        if app_id not in ledgers:
            path = accuracy._find_record(Path(after_dir), app_id)
            ledgers[app_id] = load_record(path) if path else None
        rec = ledgers[app_id]
        ledger = ([le.model_dump() for le in claim_for(rec, field).verification.loops] if rec else [])
        loops, primary = responsible(ledger)
        b, a = b_map[(app_id, field)], a_map[(app_id, field)]
        claims.append({
            "app_id": app_id, "app": a["app"], "field": field,
            "golden": a["golden"], "before_outcome": b["outcome"], "after_outcome": a["outcome"],
            "before_value": b["agent"], "after_value": a["agent"], "value_changed": b["agent"] != a["agent"],
            "transition": transition(b["outcome"], a["outcome"]),
            "responsible_loops": loops, "primary_trigger": primary, "ledger": ledger,
        })
    counts = Counter(c["transition"] for c in claims)
    fixed = [c for c in claims if c["transition"] == "fixed"]
    credited, primary_ct = Counter(), Counter()
    for c in fixed:
        for lp in c["responsible_loops"]:
            credited[lp] += 1
        primary_ct[c["primary_trigger"] or "unattributed"] += 1
    regressions = [c for c in claims if c["transition"] == "regressed"]
    return {
        "rules_sha256": after.rules_sha256, "before_pass": before.pass_name, "after_pass": after.pass_name,
        "incomplete": before.incomplete or after.incomplete,
        "unjoined": {"before_only": len(set(b_map) - set(a_map)), "after_only": len(set(a_map) - set(b_map))},
        "accuracy": {
            "before": {"overall": before.overall.get("accuracy"),
                       "per_field": {f: d.get("accuracy") for f, d in before.per_field.items()}},
            "after": {"overall": after.overall.get("accuracy"),
                      "per_field": {f: d.get("accuracy") for f, d in after.per_field.items()}},
        },
        "transitions": dict(counts),
        "errors_caught_per_loop": {
            "credited": {lp: credited.get(lp, 0) for lp in LOOPS},
            "primary_trigger": {**{lp: primary_ct.get(lp, 0) for lp in LOOPS},
                                "unattributed": primary_ct.get("unattributed", 0)},
            "fixed_total": len(fixed),
            "fixed_without_ledger": sum(1 for c in fixed if not c["responsible_loops"]),
        },
        "regressions": regressions,
        "claims": claims,
    }


def attribute(before_pass: str = "pass1", after_pass: str = "pass2", *, before_dir: Optional[Path] = None,
              after_dir: Optional[Path] = None, label_dir: Optional[Path] = None,
              ids=G.GOLDEN_IDS, out: Optional[Path] = None) -> dict[str, Any]:
    b = accuracy.score(before_pass, ids=ids, label_dir=label_dir, records_dir=before_dir)
    a = accuracy.score(after_pass, ids=ids, label_dir=label_dir, records_dir=after_dir)
    res = build(b, a, Path(after_dir) if after_dir else accuracy.default_records_dir(after_pass))
    out = Path(out) if out else accuracy.REPORTS_DIR / "attribution.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=2, default=str) + "\n")
    return res
