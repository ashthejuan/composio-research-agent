"""CLI entry: python -m agent <command>."""
from __future__ import annotations

import argparse
import sys


def _registry_check(_args: argparse.Namespace) -> int:
    from agent.registry import check_registry

    return int(check_registry())


def _smoke(_args: argparse.Namespace) -> int:
    from agent.tools.smoke import run_smoke

    return int(run_smoke())


def _composio(args: argparse.Namespace) -> int:
    import asyncio

    from agent.config import get_settings
    from agent.tools import composio_mcp as mcp

    key = get_settings().composio_api_key or ""
    if not key:
        print("error: COMPOSIO_API_KEY missing")
        return 2
    if not key.startswith("ck_"):
        print("error: `agent composio` needs a Composio consumer key (ck_...); project keys (ak_...) use the SDK")
        return 2

    async def run() -> int:
        c = mcp.get_mcp_client()
        try:
            if args.composio_cmd == "status":
                st = await c.connection_status(args.toolkits or ["exa", "firecrawl"])
                for tk, info in st.items():
                    flag = "ACTIVE" if info["active"] else "not active"
                    print(f"{tk:<12} {flag:<11} status={info['status']} accounts={info.get('accounts', 0)}")
                return 0  # informational; inactive toolkits are shown above
            rc = 0
            for tk in args.toolkits:
                link = await c.initiate_connection(tk)
                if link:
                    print(f"{tk}: open this link to connect (expires in ~10 min):\n  {link}")
                else:
                    print(f"{tk}: no redirect link returned (already connected?)")
                    rc = 1
            return rc
        finally:
            await c.aclose()

    try:
        return asyncio.run(run())
    except Exception as e:  # noqa: BLE001 - never a traceback, never a secret
        print(f"error: {type(e).__name__}: {mcp.scrub(e)[:300]}")
        return 1


def select_apps(args: argparse.Namespace):
    from agent import registry

    if getattr(args, "app", None):
        return [registry.get_app(args.app)]
    if getattr(args, "all", False):
        return registry.load_apps()
    return registry.select(ids=args.ids, category=args.category)


def _print_record(rec) -> None:
    from rich.console import Console
    from rich.table import Table

    t = Table(title=f"{rec.app} ({rec.pass_}) confidence={rec.record_confidence}")
    for c in ("field", "value", "#evidence", "first evidence URL"):
        t.add_column(c, overflow="fold")
    for name in type(rec.claims).model_fields:
        c = getattr(rec.claims, name)
        t.add_row(name, str(getattr(c.value, "model_dump", lambda: c.value)())[:80],
                  str(len(c.evidence)), c.evidence[0].url if c.evidence else "-")
    con = Console()
    con.print(t)
    for e in rec.errors:
        con.print(f"[red]error:[/red] {e}")


def _research(args: argparse.Namespace) -> int:
    import asyncio

    from agent import orchestrator

    try:
        apps = select_apps(args)
    except (KeyError, ValueError) as e:  # unknown app name/id, or a malformed --ids spec like "1-x"
        print(f"error: {e}")
        return 2
    if not apps:
        print("error: no apps matched selection")
        return 2
    m = asyncio.run(orchestrator.run_batch(
        apps, pass_=args.pass_, backend=args.backend, concurrency=args.concurrency,
        refresh=args.refresh, skip_existing=not args.no_skip,
    ))
    print(f"ok={m.ok} failed={len(m.failed)} skipped={m.skipped} cost=${m.totals['cost_usd']:.4f} "
          f"duration={m.duration_s:.1f}s")
    for f in m.failed:
        print(f"FAILED {f.id} {f.name}: {f.error}")
    if getattr(args, "app", None) and len(apps) == 1:
        rec = orchestrator.load_record(orchestrator.record_path(orchestrator.work_dir(args.pass_), apps[0]))
        _print_record(rec)
    return 1 if m.failed else 0


def _verify(args: argparse.Namespace) -> int:
    import asyncio

    from agent import orchestrator, registry

    try:
        if args.app:
            ids = [registry.get_app(args.app).id]
        elif args.ids:
            ids = [a.id for a in registry.select(ids=args.ids)]
        else:
            ids = None  # --all
    except (KeyError, ValueError) as e:
        print(f"error: {e}")
        return 2
    if ids is not None and not ids:
        print("error: no apps matched selection")
        return 2
    try:
        m = asyncio.run(orchestrator.run_verify_batch(
            args.from_, args.to, ids=ids, concurrency=args.concurrency, backend=args.backend,
            skip_existing=not args.no_skip))
    except FileNotFoundError as e:
        print(f"error: {e}")
        return 2
    print(f"ok={m.ok} failed={len(m.failed)} skipped={m.skipped} duration={m.duration_s:.1f}s "
          f"verifier_tokens={m.totals.get('verifier_tokens_in', 0)}+{m.totals.get('verifier_tokens_out', 0)}")
    for f in m.failed:
        print(f"FAILED {f.id} {f.name}: {f.error}")
    return 1 if m.failed else 0


def _loopstats(args: argparse.Namespace) -> int:
    from pathlib import Path

    from agent import loopstats

    st = loopstats.run(args.pass_, records_dir=Path(args.records_dir) if args.records_dir else None,
                       out=Path(args.out) if args.out else None)
    print(f"{'loop':<5}{'checked':>9}{'flagged':>9}{'changed':>9}")
    for lp, d in st["overall"]["loops"].items():
        print(f"{lp:<5}{d['claims_checked']:>9}{d['flagged']:>9}{d['values_changed']:>9}")
    print(f"status: {st['overall']['status_distribution']}")
    return 0


def _attribute(args: argparse.Namespace) -> int:
    from pathlib import Path

    from agent import attribution

    res = attribution.attribute(
        args.before, args.after, before_dir=Path(args.before_dir) if args.before_dir else None,
        after_dir=Path(args.after_dir) if args.after_dir else None,
        out=Path(args.out) if args.out else None)
    acc = res["accuracy"]
    print(f"overall accuracy {acc['before']['overall']} -> {acc['after']['overall']}")
    print(f"transitions: {res['transitions']}")
    print(f"errors caught per loop: {res['errors_caught_per_loop']}")
    print(f"regressions: {len(res['regressions'])}")
    return 0


def _freeze(args: argparse.Namespace) -> int:
    from agent import orchestrator

    try:
        dest = orchestrator.freeze(args.pass_)
    except orchestrator.FreezeError as e:
        print(f"error: {e}")
        return 1
    print(f"frozen to {dest}")
    return 0


def _status(args: argparse.Namespace) -> int:
    from agent import orchestrator

    st = orchestrator.status(args.pass_)
    print(f"records: {st['count']}  invalid: {len(st['invalid'])}  failed: {len(st['failed'])}")
    for f in st["failed"]:
        print(f"FAILED {f['id']} {f['name']}: {f['error']}")
    for f in st["invalid"]:
        print(f"INVALID {f}")
    for k, v in sorted(st["unknown_by_field"].items(), key=lambda kv: -kv[1]):
        print(f"unknown {k}: {v}")
    return 1 if st["failed"] else 0


def _score(args: argparse.Namespace) -> int:
    from pathlib import Path

    from rich.console import Console
    from rich.table import Table

    from agent import accuracy

    con = Console()
    drafts = args.include_drafts
    out = Path(args.out) if args.out else (
        accuracy.provisional_report_path(args.pass_) if drafts
        else accuracy.official_report_path(args.pass_))
    if drafts:
        con.print("[bold white on red] PROVISIONAL — drafts not human-verified [/]")
        if accuracy.PROVISIONAL_MARK not in out.name:
            print("error: --include-drafts output name must contain 'PROVISIONAL' "
                  "(never an official report path)")
            return 2
    rep = accuracy.score(args.pass_, records_dir=Path(args.records_dir) if args.records_dir else None,
                         include_drafts=drafts)
    accuracy.write_report(rep, out)

    t = Table(title=f"Accuracy {args.pass_}" + (" (PROVISIONAL)" if drafts else ""))
    for c in ("field", "evaluated", "excl", "correct", "wrong", "partial", "abstained", "acc %"):
        t.add_column(c, justify="right" if c != "field" else "left")

    def row(name, d):
        t.add_row(name, str(d["evaluated"]), str(d["excluded"]), str(d["correct"]), str(d["wrong"]),
                  str(d["partial"]), str(d["abstained"]), "-" if d["accuracy"] is None else f"{d['accuracy']:.1f}")

    for f, d in rep.per_field.items():
        row(f, d)
    row("OVERALL", rep.overall)
    con.print(t)
    con.print(f"strict auth accuracy: {rep.strict_auth_accuracy}  apps scored: {rep.n_apps}")
    if rep.incomplete:
        con.print(f"[yellow]INCOMPLETE[/yellow] labels missing: {rep.labels_missing} "
                  f"records missing: {rep.records_missing}")
    con.print(f"wrote {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent")
    sub = p.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("registry", help="app registry commands")
    reg_sub = reg.add_subparsers(dest="registry_cmd", required=True)
    reg_sub.add_parser("check", help="validate data/apps.json").set_defaults(func=_registry_check)

    sub.add_parser("smoke", help="Composio search + scrape smoke test").set_defaults(func=_smoke)

    co = sub.add_parser("composio", help="Composio Connect (consumer key) connections")
    co_sub = co.add_subparsers(dest="composio_cmd", required=True)
    cs = co_sub.add_parser("status", help="connection status per toolkit (default: exa firecrawl)")
    cs.add_argument("toolkits", nargs="*")
    cs.set_defaults(func=_composio)
    cc = co_sub.add_parser("connect", help="print an auth link for each toolkit")
    cc.add_argument("toolkits", nargs="+")
    cc.set_defaults(func=_composio)

    r = sub.add_parser("research", help="run the research pass over selected apps")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--app", help="app name or id")
    g.add_argument("--ids", help='id list/ranges, e.g. "1-10,15"')
    g.add_argument("--category", help="category name (substring ok)")
    g.add_argument("--all", action="store_true", help="all 100 apps")
    r.add_argument("--pass", dest="pass_", default="pass1")
    r.add_argument("--backend", choices=["composio", "composio_sdk", "direct"], default=None)
    r.add_argument("--concurrency", type=int, default=None)
    r.add_argument("--refresh", action="store_true", help="bypass tool cache and redo existing records")
    r.add_argument("--no-skip", action="store_true", help="re-run apps that already have a record")
    r.set_defaults(func=_research)

    v = sub.add_parser("verify", help="run verification loops L1-L6 over frozen records")
    vg = v.add_mutually_exclusive_group(required=True)
    vg.add_argument("--app", help="app name or id")
    vg.add_argument("--ids", help='id list/ranges, e.g. "1-10,15"')
    vg.add_argument("--all", action="store_true", help="all apps")
    v.add_argument("--from", dest="from_", default="pass1")
    v.add_argument("--to", default="pass2", help="run label; records are always written as pass2")
    v.add_argument("--backend", choices=["composio", "composio_sdk", "direct"], default=None)
    v.add_argument("--concurrency", type=int, default=None)
    v.add_argument("--no-skip", action="store_true", help="re-verify apps that already have a record")
    v.set_defaults(func=_verify)

    ls = sub.add_parser("loopstats", help="per-loop statistics from claim ledgers")
    ls.add_argument("--pass", dest="pass_", default="pass2")
    ls.add_argument("--records-dir", default=None)
    ls.add_argument("--out", default=None)
    ls.set_defaults(func=_loopstats)

    at = sub.add_parser("attribute", help="attribute accuracy changes to loops")
    at.add_argument("--before", default="pass1")
    at.add_argument("--after", default="pass2")
    at.add_argument("--before-dir", default=None)
    at.add_argument("--after-dir", default=None)
    at.add_argument("--out", default=None)
    at.set_defaults(func=_attribute)

    fz = sub.add_parser("freeze", help="freeze work records into data/<pass>/")
    fz.add_argument("--pass", dest="pass_", default="pass1")
    fz.set_defaults(func=_freeze)

    st = sub.add_parser("status", help="summarise work records")
    st.add_argument("--pass", dest="pass_", default="pass1")
    st.set_defaults(func=_status)

    sc = sub.add_parser("score", help="score a pass against the golden set (FR-08)")
    sc.add_argument("--pass", dest="pass_", default="pass1")
    sc.add_argument("--out", default=None)
    sc.add_argument("--records-dir", default=None, help="override records dir (e.g. a work dir)")
    sc.add_argument("--include-drafts", action="store_true",
                    help="score against AI drafts: PROVISIONAL, written to a separate file")
    sc.set_defaults(func=_score)

    # TODO later phases: research, verify, score, analyze, site
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
