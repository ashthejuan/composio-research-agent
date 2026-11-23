"""Batch orchestrator: concurrent research runs, retries, resume, manifest, freeze (PRD NFR, FR-05)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from agent.config import get_settings
from agent.registry import App
from agent.schema import AppRecord, RunInfo, dump_record, load_record

RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)  # patchable in tests
MAX_ATTEMPTS = 2
EXPECTED_COUNT = 100
FAIL_PREFIX = "research failed:"
VERIFY_FAIL_PREFIX = "verify failed:"
RECORD_PASSES = ("pass1", "pass2", "final")  # AppRecord.pass_ values; other --pass names are run labels
_KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{10,}|ak_[A-Za-z0-9_\-]{6,}|[A-Za-z0-9_\-]{32,})")
_SECRET_ENV = ("COMPOSIO_API_KEY", "OPENAI_API_KEY", "META_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY")


def _scrub(msg: object) -> str:
    try:
        from agent.tools.smoke import scrub

        return scrub(msg)
    except Exception:  # noqa: BLE001
        text = str(msg)
        for name in _SECRET_ENV:
            val = os.environ.get(name)
            if val and len(val) >= 6:
                text = text.replace(val, "[REDACTED]")
        return _KEYLIKE.sub("[REDACTED]", text)[:300]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def record_path(out_dir: Path, app: App) -> Path:
    return out_dir / f"{app.id:03d}-{slugify(app.name)}.json"


def work_dir(pass_: str) -> Path:
    return get_settings().DATA_DIR / "work" / pass_


def git_commit() -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
            cwd=get_settings().ROOT,
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:  # noqa: BLE001
        return None


GIT_INPUT_PATHS: tuple[str, ...] = ("agent", "pyproject.toml", "data/apps.json")  # what shapes output


def git_dirty() -> Optional[bool]:
    """True if code/registry inputs (tracked or untracked) differ from HEAD: git_commit alone would then
    claim the records came from code they did not come from. Run outputs (data/reports) are ignored."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain", "--", *GIT_INPUT_PATHS], capture_output=True, text=True, timeout=10,
            cwd=get_settings().ROOT,
        )
        return bool(r.stdout.strip()) if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ manifest
class FailedApp(BaseModel):
    id: int
    name: str
    error: str


class AppSummary(BaseModel):
    id: int
    name: str
    unknown_fields_count: int
    errors_count: int
    record_confidence: str
    composio_toolkit: str


class Manifest(BaseModel):
    pass_: str = Field(alias="pass")  # run label (pass1, pass2, final, or e.g. dryrun)
    record_pass: str = "pass1"  # AppRecord.pass_ written by this run
    started_at: datetime
    finished_at: datetime
    duration_s: float
    backend: str
    extractor_model: str
    concurrency: int
    git_commit: Optional[str] = None
    git_dirty: Optional[bool] = None  # working tree differed from git_commit when the run started
    apps_total: int
    ok: int
    failed: list[FailedApp] = Field(default_factory=list)
    skipped: int = 0
    totals: dict[str, Any] = Field(default_factory=dict)
    apps: list[AppSummary] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


def unknown_fields(record: AppRecord) -> list[str]:
    return [n for n in type(record.claims).model_fields if getattr(record.claims, n).is_unknown()]


def _summary(app: App, rec: AppRecord) -> AppSummary:
    return AppSummary(
        id=app.id, name=app.name, unknown_fields_count=len(unknown_fields(rec)),
        errors_count=len(rec.errors), record_confidence=rec.record_confidence,
        composio_toolkit=rec.composio_toolkit.value,
    )


def record_pass(pass_: str) -> str:
    """Record-level pass for a run label: `--pass dryrun` writes pass1-shaped records to work/dryrun."""
    return pass_ if pass_ in RECORD_PASSES else "pass1"


def is_failed(rec: AppRecord) -> bool:
    return any(e.startswith((FAIL_PREFIX, VERIFY_FAIL_PREFIX)) for e in rec.errors)


def _valid_existing(path: Path) -> Optional[AppRecord]:
    """An existing record worth keeping on resume: valid and not a failure placeholder."""
    if not path.exists():
        return None
    try:
        rec = load_record(path)
    except Exception:  # noqa: BLE001
        return None
    return None if is_failed(rec) else rec  # failed apps are always retried on the next run


# ------------------------------------------------------------------- batch
async def run_batch(
    apps: list[App],
    *,
    pass_: str = "pass1",
    backend: Optional[str] = None,
    concurrency: Optional[int] = None,
    out_dir: Optional[Path] = None,
    refresh: bool = False,
    skip_existing: bool = True,
    reports_dir: Optional[Path] = None,
    worker: Optional[Callable[..., Awaitable[AppRecord]]] = None,
    fallback: Optional[Callable[[App, str], AppRecord]] = None,
    fail_prefix: str = FAIL_PREFIX,
    record_pass_: Optional[str] = None,
) -> Manifest:
    """``worker(app, tools, rpass) -> AppRecord`` defaults to research; ``fallback(app, err)`` builds the
    record written when every attempt failed (default: an all-unknown record)."""
    from agent.cache import reset_cache

    s = get_settings()
    backend = backend or s.tool_backend
    conc = concurrency or s.concurrency
    out = Path(out_dir) if out_dir else work_dir(pass_)
    out.mkdir(parents=True, exist_ok=True)
    rpass = record_pass_ or record_pass(pass_)
    prev_refresh = os.environ.get("REFRESH")
    if refresh:
        os.environ["REFRESH"] = "1"
        reset_cache()
    try:
        return await _run_batch(apps, pass_=pass_, rpass=rpass, backend=backend, conc=conc, out=out,
                                refresh=refresh, skip_existing=skip_existing, reports_dir=reports_dir,
                                worker=worker, fallback=fallback, fail_prefix=fail_prefix)
    finally:
        if refresh:  # do not leak cache bypass into later runs in the same process
            if prev_refresh is None:
                os.environ.pop("REFRESH", None)
            else:
                os.environ["REFRESH"] = prev_refresh
            reset_cache()


async def _run_batch(apps: list[App], *, pass_: str, rpass: str, backend: str, conc: int, out: Path,
                     refresh: bool, skip_existing: bool, reports_dir: Optional[Path],
                     worker: Optional[Callable[..., Awaitable[AppRecord]]] = None,
                     fallback: Optional[Callable[[App, str], AppRecord]] = None,
                     fail_prefix: str = FAIL_PREFIX) -> Manifest:
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

    from agent.cache import get_cache
    from agent.tools.base import get_tools

    s = get_settings()
    started = _now()
    commit, dirty = git_commit(), git_dirty()  # code state the run actually used (before it writes)
    tools = get_tools(backend)
    if worker is None:
        from agent import research

        async def worker(app, tools, rpass):  # noqa: F811
            return await research.research_app(app, tools, pass_=rpass)
    verifying = fail_prefix == VERIFY_FAIL_PREFIX
    sem = asyncio.Semaphore(conc)

    results: dict[int, AppRecord] = {}
    ran: set[int] = set()
    failed: list[FailedApp] = []
    skipped = 0

    with Progress(
        TextColumn("[bold]{task.description}"), BarColumn(), MofNCompleteColumn(),
        TextColumn("{task.fields[status]}"), TimeElapsedColumn(),
    ) as progress:
        task = progress.add_task("research", total=len(apps), status="ok=0 failed=0")

        def tick(label: str) -> None:
            progress.update(
                task, advance=1, description=label[:30].ljust(30),
                status=f"ok={len(results) - len(failed)} failed={len(failed)}",
            )

        async def one(app: App) -> None:
            nonlocal skipped
            path = record_path(out, app)
            if skip_existing and not refresh:
                existing = _valid_existing(path)
                if existing is not None:
                    results[app.id] = existing
                    skipped += 1
                    tick(app.name)
                    return
            async with sem:
                progress.update(task, description=f"{app.name[:30]}".ljust(30))
                rec: Optional[AppRecord] = None
                err = ""
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        rec = await worker(app, tools, rpass)
                        break
                    except Exception as e:  # noqa: BLE001
                        if verifying:
                            from agent import verify as verify_mod

                            check_fatal = getattr(verify_mod, "is_fatal", None)
                            if callable(check_fatal) and check_fatal(e):
                                raise
                        err = f"{type(e).__name__}: {_scrub(e)}"
                        if attempt + 1 < MAX_ATTEMPTS:
                            await asyncio.sleep(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                if rec is None:
                    if fallback is not None:
                        rec = fallback(app, err)
                    else:
                        now = _now()
                        rec = AppRecord.unknown(
                            app.id, app.name, rpass,
                            RunInfo(model=s.extractor_model, started_at=now, finished_at=now, backend=backend),
                        )
                        rec.errors = [f"{fail_prefix} {err}"]
                    failed.append(FailedApp(id=app.id, name=app.name, error=err))
                dump_record(rec, path)
                results[app.id] = rec
                ran.add(app.id)
                tick(app.name)

        await asyncio.gather(*(one(a) for a in apps))

    finished = _now()
    run_recs = [results[i] for i in ran]
    cache_stats = get_cache().stats()
    manifest = Manifest(
        pass_=pass_, record_pass=rpass, started_at=started, finished_at=finished,
        duration_s=round((finished - started).total_seconds(), 3),
        backend=backend, extractor_model=s.extractor_model, concurrency=conc,
        git_commit=commit, git_dirty=dirty, apps_total=len(apps),
        ok=len(ran) - len(failed), failed=sorted(failed, key=lambda f: f.id), skipped=skipped,
        totals={
            "tool_calls": sum(r.run.tool_calls for r in run_recs),
            "tokens_in": sum(r.run.tokens_in for r in run_recs),
            "tokens_out": sum(r.run.tokens_out for r in run_recs),
            "cost_usd": round(sum(r.run.cost_usd for r in run_recs), 6),
            # cost is 0 unless EXTRACTOR_PRICE_IN/OUT (USD per 1M tokens) are set: say so explicitly
            "cost_priced": bool(os.environ.get("EXTRACTOR_PRICE_IN") or os.environ.get("EXTRACTOR_PRICE_OUT")),
            "cache": cache_stats,
            "tool_backend_counts": _backend_counts(tools, backend, run_recs, catalog_calls=0 if verifying else len(run_recs)),
            **(_verifier_totals(run_recs) if verifying else {}),
        },
        apps=[_summary(a, results[a.id]) for a in sorted(apps, key=lambda a: a.id) if a.id in results],
    )
    rdir = Path(reports_dir) if reports_dir else s.REPORTS_DIR
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / f"run_{pass_}.json").write_text(manifest.model_dump_json(indent=2, by_alias=True) + "\n")
    return manifest


def _verifier_totals(recs: list[AppRecord]) -> dict[str, Any]:
    """Verifier-side totals read tolerantly from record.run (verify.py decides what it fills in)."""
    models = sorted({m for r in recs if (m := getattr(r.run, "verifier_model", None))})
    entries = [le for r in recs for n in type(r.claims).model_fields
               for le in getattr(r.claims, n).verification.loops]
    return {
        "verifier_models": models,
        "verifier_tokens_in": sum(r.run.verifier_tokens_in for r in recs),
        "verifier_tokens_out": sum(r.run.verifier_tokens_out for r in recs),
        "l6_extractor_tokens_in": sum(r.run.tokens_in for r in recs),
        "l6_extractor_tokens_out": sum(r.run.tokens_out for r in recs),
        "l6_tool_calls": sum(r.run.l6_tool_calls for r in recs),
        "l6_rerun_claims": sum(r.run.l6_rerun_claims for r in recs),
        "l6_changed_claims": sum(1 for le in entries if le.loop == "L6" and le.result in ("changed", "set_unknown")),
        "ledger_entries": len(entries),
    }


def _backend_counts(tools, backend: Optional[str], run_recs: list[AppRecord],
                    *, catalog_calls: Optional[int] = None) -> dict[str, int]:
    """Search/scrape calls per backend. Fallback wrappers (TOOL_FALLBACK=direct) count exactly; otherwise
    every search/scrape call went through the one resolved backend (tool_calls minus catalog calls)."""
    from agent.tools.base import resolve_backend
    from agent.tools.fallback import backend_counts

    counted = backend_counts(tools[0], tools[1])
    if counted is not None:
        return counted
    try:
        label = resolve_backend(backend)
    except ValueError:
        label = str(backend)
    sub = len(run_recs) if catalog_calls is None else catalog_calls
    return {label: max(0, sum(r.run.tool_calls for r in run_recs) - sub), "fallbacks": 0}


async def run_verify_batch(
    from_pass: str = "pass1",
    to_label: str = "pass2",
    ids: Optional[list[int]] = None,
    concurrency: Optional[int] = None,
    backend: Optional[str] = None,
    *,
    apps: Optional[list[App]] = None,
    src_dir: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    skip_existing: bool = True,
    reports_dir: Optional[Path] = None,
) -> Manifest:
    """Verify frozen ``from_pass`` records into data/work/<to_label>/ (records carry pass "pass2")."""
    from agent import registry

    src = Path(src_dir) if src_dir else get_settings().DATA_DIR / from_pass
    if apps is None:
        apps = registry.load_apps()
        if ids is not None:
            want = set(ids)
            apps = [a for a in apps if a.id in want]
    originals: dict[int, AppRecord] = {}
    for a in apps:
        hits = sorted(src.glob(f"{a.id:03d}-*.json"))
        if not hits:
            raise FileNotFoundError(f"no {from_pass} record for app {a.id} in {src}")
        originals[a.id] = load_record(hits[0])

    async def worker(app: App, tools: tuple, rpass: str) -> AppRecord:
        from agent import verify

        rec = await verify.verify_app(originals[app.id].model_copy(deep=True), app, tools)
        return rec.model_copy(update={"pass_": "pass2"})

    def fallback(app: App, err: str) -> AppRecord:
        rec = originals[app.id].model_copy(deep=True)
        rec.pass_ = "pass2"
        rec.run = rec.run.model_copy(update={
            "tool_calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
            "verifier_tokens_in": 0, "verifier_tokens_out": 0, "l6_tool_calls": 0, "l6_rerun_claims": 0,
        })
        rec.errors = [*rec.errors, f"{VERIFY_FAIL_PREFIX} {err}"]
        return rec

    from agent.tools import browser

    try:
        return await run_batch(
            apps, pass_=to_label, backend=backend, concurrency=concurrency, out_dir=out_dir,
            skip_existing=skip_existing, reports_dir=reports_dir,
            worker=worker, fallback=fallback, fail_prefix=VERIFY_FAIL_PREFIX, record_pass_="pass2",
        )
    finally:
        await browser.close_browser()


# ------------------------------------------------------------------- freeze
class FreezeError(Exception):
    pass


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def freeze(pass_: str, *, src: Optional[Path] = None, dest: Optional[Path] = None) -> Path:
    """Copy work records into the immutable data/<pass_>/ directory (FR-05)."""
    if pass_ not in RECORD_PASSES:
        raise FreezeError(f"{pass_!r} is a run label, not a snapshot pass {RECORD_PASSES}")
    src = Path(src) if src else work_dir(pass_)
    dest = Path(dest) if dest else get_settings().DATA_DIR / pass_
    if dest.exists() and any(dest.glob("*.json")):
        raise FreezeError(f"{dest} already contains .json files; snapshots are never overwritten")
    files = sorted(src.glob("*.json")) if src.exists() else []
    problems: list[str] = []
    ids: list[int] = []
    for f in files:
        try:
            rec = load_record(f)
            ids.append(rec.app_id)
            if rec.pass_ != pass_:
                problems.append(f"{f.name}: record pass {rec.pass_!r} != {pass_!r}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"{f.name}: invalid record ({type(e).__name__}: {_scrub(e)})")
    if len(files) != EXPECTED_COUNT:
        problems.append(f"expected {EXPECTED_COUNT} records, found {len(files)} in {src}")
    if len(set(ids)) != len(ids):
        problems.append("duplicate app ids")
    if not problems and sorted(ids) != list(range(1, EXPECTED_COUNT + 1)):
        problems.append("app ids are not exactly 1..100")
    if problems:
        raise FreezeError("cannot freeze:\n- " + "\n- ".join(problems))
    dest.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for f in files:
        (dest / f.name).write_bytes(f.read_bytes())
        hashes[f.name] = _sha256(dest / f.name)
        (dest / f.name).chmod(0o444)  # snapshots are never edited after freezing (FR-05)
    meta = {
        "pass": pass_, "frozen_at": _now().isoformat(), "git_commit": git_commit(),
        "git_dirty": git_dirty(), "file_count": len(files), "sha256": hashes,
    }
    (dest / "_frozen.json").write_text(json.dumps(meta, indent=2) + "\n")
    (dest / "_frozen.json").chmod(0o444)
    return dest


# ------------------------------------------------------------------- status
def status(pass_: str, src: Optional[Path] = None) -> dict[str, Any]:
    src = Path(src) if src else work_dir(pass_)
    out: dict[str, Any] = {"count": 0, "invalid": [], "failed": [], "unknown_by_field": {}}
    for f in sorted(src.glob("*.json")) if src.exists() else []:
        try:
            rec = load_record(f)
        except Exception:  # noqa: BLE001
            out["invalid"].append(f.name)
            continue
        out["count"] += 1
        if is_failed(rec):
            out["failed"].append({"id": rec.app_id, "name": rec.app, "error": rec.errors[0]})
        for name in unknown_fields(rec):
            out["unknown_by_field"][name] = out["unknown_by_field"].get(name, 0) + 1
    return out
