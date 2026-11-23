"""Golden-set constants and label loading (FR-07).

Confirmed labels live at ``data/golden/<id:03d>.json``; AI drafts at
``data/golden/drafts/<id:03d>.json`` with ``labeller`` starting ``draft:``.
Only confirmed (human) labels are ever scored, unless drafts are requested
explicitly for a PROVISIONAL report.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, get_args

from agent.schema import (
    GOLDEN_FIELDS, ApiType, AuthMethod, BlockerType, CredentialAccess, GoldenField,
    GoldenLabel, ProductionGate, YesNoFound,
)

GOLDEN_IDS: tuple[int, ...] = (
    2, 10, 11, 15, 21, 28, 32, 35, 49, 50, 53, 58, 61, 67, 74, 75, 81, 90, 91, 98,
)
HOLDOUT_IDS: tuple[int, ...] = (7, 30, 46, 84, 100)

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "data" / "golden"
DRAFT_PREFIX = "draft:"
REVIEW_LOG_NAME = "_review_log.jsonl"
UNKNOWN = "unknown"
SET_FIELDS = ("auth_methods", "api_type")
# Legal golden values per evaluated field ("unknown" = docs do not establish it).
FIELD_VALUES: dict[str, tuple[str, ...]] = {
    "auth_methods": get_args(AuthMethod), "credential_access": get_args(CredentialAccess),
    "production_gate": get_args(ProductionGate), "api_type": get_args(ApiType),
    "mcp_official": get_args(YesNoFound), "main_blocker_type": get_args(BlockerType),
}
assert set(FIELD_VALUES) == set(GOLDEN_FIELDS)


class InvalidLabel(ValueError):
    """A golden label whose values do not fit the field enums/shapes."""


def field_value_error(field: str, gf: GoldenField) -> Optional[str]:
    """Return why ``gf`` is not a legal golden value for ``field`` (None if OK).

    Set fields must be a non-empty list of distinct allowed values, with
    ``unknown`` only as exactly ``["unknown"]``; scalar fields must be one
    allowed string. Ambiguous fields carry no value to check.
    """
    if gf.status == "ambiguous":
        return None
    allowed, v = FIELD_VALUES[field], gf.value
    if field in SET_FIELDS:
        if not isinstance(v, list) or not v:
            return f"{field}: set field needs a non-empty list, got {v!r}"
        if len(set(v)) != len(v):
            return f"{field}: duplicate values {v!r}"
        if UNKNOWN in v and len(v) > 1:
            return f"{field}: 'unknown' must not be mixed with other values"
        bad = [x for x in v if x not in allowed]
    else:
        if not isinstance(v, str):
            return f"{field}: scalar field needs a string, got {v!r}"
        bad = [] if v in allowed else [v]
    return f"{field}: invalid value(s) {bad!r}; allowed {allowed}" if bad else None


def validate_label(label: GoldenLabel, app_id: Optional[int] = None) -> GoldenLabel:
    """Raise InvalidLabel if any field value is illegal or the id mismatches."""
    if app_id is not None and label.app_id != app_id:
        raise InvalidLabel(f"label app_id {label.app_id} != expected {app_id}")
    errs = [e for f in GOLDEN_FIELDS if (e := field_value_error(f, label.fields[f]))]
    if errs:
        raise InvalidLabel(f"label {label.app_id:03d}: " + "; ".join(errs))
    return label


def draft_path(app_id: int, golden_dir: Optional[Path] = None) -> Path:
    return Path(golden_dir or GOLDEN_DIR) / "drafts" / f"{app_id:03d}.json"


def confirmed_path(app_id: int, golden_dir: Optional[Path] = None) -> Path:
    return Path(golden_dir or GOLDEN_DIR) / f"{app_id:03d}.json"


def review_log_path(golden_dir: Optional[Path] = None) -> Path:
    return Path(golden_dir or GOLDEN_DIR) / REVIEW_LOG_NAME


def is_draft(label: GoldenLabel) -> bool:
    return label.labeller.startswith(DRAFT_PREFIX)


def _load(path: Path) -> GoldenLabel:
    return GoldenLabel.model_validate(json.loads(path.read_text()))


def load_labels(
    confirmed_only: bool = True,
    ids: Optional[tuple[int, ...] | list[int]] = None,
    golden_dir: Optional[Path] = None,
) -> dict[int, GoldenLabel]:
    """Load labels by app id.

    ``confirmed_only=True``: only confirmed human labels (a file whose labeller
    starts with ``draft:`` is skipped even at the confirmed path).
    ``confirmed_only=False``: load drafts from ``drafts/`` (nothing else).
    """
    out: dict[int, GoldenLabel] = {}
    for i in ids if ids is not None else GOLDEN_IDS:
        p = confirmed_path(i, golden_dir) if confirmed_only else draft_path(i, golden_dir)
        if not p.exists():
            continue
        label = _load(p)
        if confirmed_only and is_draft(label):
            continue
        out[i] = validate_label(label, app_id=i)
    return out
