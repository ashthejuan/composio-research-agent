#!/usr/bin/env python
"""Interactive golden-label tool (FR-07).

    python scripts/label.py <app name|id> [--from-draft] [--labeller NAME]

Blind by design: this tool never reads or shows the agent's pass1/pass2 output.
With --from-draft the AI draft is shown per field so a human can accept, edit
or mark ambiguous after checking the cited docs. Honest process = interactive.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import golden as G  # noqa: E402
from agent.registry import App, get_app  # noqa: E402
from agent.schema import GOLDEN_FIELDS, GoldenField, GoldenLabel  # noqa: E402

SET_FIELDS = G.SET_FIELDS
UNKNOWN_QUOTE = "not established by public docs"
# Values a human may type; golden "unknown" is entered with 'u' only.
ALLOWED: dict[str, tuple[str, ...]] = {
    f: tuple(v for v in vals if v != G.UNKNOWN) for f, vals in G.FIELD_VALUES.items()
}

InputFn = Callable[[str], str]
PrintFn = Callable[[str], None]


def parse_value(field: str, raw: str) -> Optional[str | list[str]]:
    """Validate a typed value; None if invalid."""
    allowed = ALLOWED[field]
    if field in SET_FIELDS:
        vals = [v.strip().lower() for v in raw.split(",") if v.strip()]
        if not vals or len(set(vals)) != len(vals) or any(v not in allowed for v in vals):
            return None
        return sorted(vals)
    raw = raw.strip().lower()
    return raw if raw in allowed else None


def _ask(prompt: str, inp: InputFn, ok: Callable[[str], bool], pr: PrintFn, err: str) -> str:
    while True:
        s = inp(prompt).strip()
        if ok(s):
            return s
        pr(err)


def _is_url(s: str) -> bool:
    return s.lower().startswith(("http://", "https://")) and len(s) >= 10


def _ask_value(field: str, inp: InputFn, pr: PrintFn, current=None):
    if current is not None and G.field_value_error(
            field, GoldenField(value=current, url="https://x.invalid", quote="q")):
        current = None  # never offer to keep an illegal draft value
    shown = f" [Enter keeps {current}]" if current else ""
    while True:
        s = inp(f"  value{shown}: ").strip()
        if not s and current:
            return current
        v = parse_value(field, s)
        if v is not None:
            return v
        pr(f"  invalid; allowed: {', '.join(ALLOWED[field])}"
           + (" (comma-separated)" if field in SET_FIELDS else ""))


def _ask_ambiguous(inp: InputFn, pr: PrintFn) -> GoldenField:
    note = _ask("  ambiguous note: ", inp, bool, pr, "  a note is required")
    return GoldenField(status="ambiguous", note=note)


def _ask_unknown(field: str, inp: InputFn, pr: PrintFn) -> GoldenField:
    url = _ask("  url searched: ", inp, _is_url, pr, "  need an http(s) url")
    note = _ask("  note: ", inp, bool, pr, "  a note is required")
    return GoldenField(value=["unknown"] if field in SET_FIELDS else "unknown",
                       url=url, quote=UNKNOWN_QUOTE, note=note)


def prompt_field(field: str, inp: InputFn, pr: PrintFn,
                 draft: Optional[GoldenField] = None) -> tuple[GoldenField, str]:
    """Return (GoldenField, action) with action in accepted/edited/ambiguous."""
    pr(f"\n== {field} ==")
    pr(f"  allowed: {', '.join(ALLOWED[field])}"
       + ("  (comma-separated set)" if field in SET_FIELDS else ""))
    pr("  a = ambiguous, u = golden unknown (docs do not establish it)")
    if draft is not None:
        pr(f"  DRAFT status={draft.status} value={draft.value}\n  url={draft.url}\n"
           f"  quote={draft.quote}\n  note={draft.note}")
        bad = G.field_value_error(field, draft)
        if bad:
            pr(f"  DRAFT INVALID ({bad}); it cannot be accepted, edit it")
        while True:
            c = inp("  [Enter]=accept  e=edit  a=ambiguous  u=unknown: ").strip().lower()
            if c == "" and not bad:
                return draft, "accepted"
            if c == "a":
                return _ask_ambiguous(inp, pr), "ambiguous"
            if c == "u":
                return _ask_unknown(field, inp, pr), "edited"
            if c == "e":
                keep = draft.status == "labelled"
                val = _ask_value(field, inp, pr, draft.value if keep else None)
                url = inp(f"  url [Enter keeps {draft.url}]: ").strip() or draft.url or ""
                while not _is_url(url):
                    url = inp("  url (http/https): ").strip()
                quote = inp("  quote [Enter keeps]: ").strip() or draft.quote or ""
                while not quote:
                    quote = inp("  quote: ").strip()
                return GoldenField(value=val, url=url, quote=quote,
                                   note=draft.note), "edited"
            pr("  choose Enter, e, a or u")
    while True:
        c = inp("  a=ambiguous  u=unknown  or Enter to label: ").strip().lower()
        if c == "a":
            return _ask_ambiguous(inp, pr), "ambiguous"
        if c == "u":
            return _ask_unknown(field, inp, pr), "edited"
        if c == "":
            break
        pr("  choose a, u or Enter")
    val = _ask_value(field, inp, pr)
    url = _ask("  url: ", inp, _is_url, pr, "  need an http(s) url")
    quote = _ask("  quote (verbatim): ", inp, bool, pr, "  a quote is required")
    return GoldenField(value=val, url=url, quote=quote), "edited"


def show_app(app: App, pr: PrintFn) -> None:
    pr(f"\n#{app.id} {app.name}  [{app.category}]")
    pr(f"hint:    {app.hint}")
    pr(f"website: {app.website}")
    pr(f"official_domains: {', '.join(app.official_domains)}")
    pr("(nothing is opened automatically; click the URLs above)")


def build_label(app: App, labeller: str, inp: InputFn, pr: PrintFn,
                draft: Optional[GoldenLabel] = None,
                now: Optional[datetime] = None) -> tuple[GoldenLabel, dict]:
    t0 = time.monotonic()
    fields: dict[str, GoldenField] = {}
    counts = {"fields_accepted": 0, "fields_edited": 0, "fields_ambiguous": 0}
    for f in GOLDEN_FIELDS:
        gf, action = prompt_field(f, inp, pr, draft.fields[f] if draft else None)
        fields[f] = gf
        counts[{"accepted": "fields_accepted", "edited": "fields_edited",
                "ambiguous": "fields_ambiguous"}[action]] += 1
    label = GoldenLabel(app_id=app.id, app=app.name, labeller=labeller,
                        labelled_at=now or datetime.now(timezone.utc), fields=fields)
    log = {"app_id": app.id, "labeller": labeller,
           "seconds_spent": round(time.monotonic() - t0, 1), **counts,
           "from_draft": draft is not None, "mode": "interactive",
           "logged_at": label.labelled_at.isoformat()}
    return label, log


def accept_all(draft: GoldenLabel, labeller: str, now: Optional[datetime] = None):
    amb = sum(f.status == "ambiguous" for f in draft.fields.values())
    label = GoldenLabel(app_id=draft.app_id, app=draft.app, labeller=labeller,
                        labelled_at=now or datetime.now(timezone.utc), fields=draft.fields)
    log = {"app_id": draft.app_id, "labeller": labeller, "seconds_spent": 0.0,
           "fields_accepted": len(draft.fields) - amb, "fields_edited": 0,
           "fields_ambiguous": amb, "from_draft": True, "mode": "accept_all_drafts",
           "logged_at": label.labelled_at.isoformat()}
    return label, log


def save(label: GoldenLabel, log: dict, golden_dir: Optional[Path] = None,
         force: bool = False) -> Path:
    path = G.confirmed_path(label.app_id, golden_dir)
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; use --force to overwrite")
    if label.labeller.startswith(G.DRAFT_PREFIX):
        raise ValueError("a confirmed label's labeller must not start with 'draft:'")
    G.validate_label(GoldenLabel.model_validate(label.model_dump()))  # schema + enums
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(label.model_dump_json(indent=2) + "\n")
    with G.review_log_path(golden_dir).open("a") as fh:
        fh.write(json.dumps(log) + "\n")
    return path


def default_labeller() -> str:
    try:
        r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "human"
    except Exception:  # noqa: BLE001
        return "human"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("app", help="app name or id")
    ap.add_argument("--from-draft", action="store_true")
    ap.add_argument("--labeller", default=None)
    ap.add_argument("--force", action="store_true", help="overwrite an existing confirmed label")
    ap.add_argument("--accept-all-drafts", action="store_true",
                    help="NON-INTERACTIVE; testing only; requires --i-reviewed-these")
    ap.add_argument("--i-reviewed-these", action="store_true")
    a = ap.parse_args(argv)
    try:
        app = get_app(int(a.app) if a.app.isdigit() else a.app)
    except KeyError as e:
        print(f"error: {e}")
        return 2
    labeller = a.labeller or default_labeller()
    target = G.confirmed_path(app.id)
    if target.exists() and not a.force:  # fail before the human spends time labelling
        print(f"error: {target} exists; use --force to overwrite")
        return 1
    draft = None
    if a.from_draft or a.accept_all_drafts:
        dp = G.draft_path(app.id)
        if not dp.exists():
            print(f"error: no draft at {dp}")
            return 2
        draft = GoldenLabel.model_validate(json.loads(dp.read_text()))
        if draft.app_id != app.id:
            print(f"error: draft {dp} is for app {draft.app_id}, not {app.id}")
            return 2
    if a.accept_all_drafts:
        if not a.i_reviewed_these:
            print("error: --accept-all-drafts requires --i-reviewed-these")
            return 2
        print("!" * 70 + "\nWARNING: accepting AI drafts WITHOUT field-by-field review.\n"
              "The honest process is interactive (--from-draft). Do not use for the\n"
              "published golden set unless you really checked every draft.\n" + "!" * 70)
        label, log = accept_all(draft, labeller)
    else:
        show_app(app, print)
        label, log = build_label(app, labeller, input, print, draft)
    try:
        path = save(label, log, force=a.force)
    except (FileExistsError, ValueError) as e:  # ValueError incl. G.InvalidLabel
        print(f"error: {e}")
        return 1
    print(f"saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
