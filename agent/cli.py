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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent")
    sub = p.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("registry", help="app registry commands")
    reg_sub = reg.add_subparsers(dest="registry_cmd", required=True)
    reg_sub.add_parser("check", help="validate data/apps.json").set_defaults(func=_registry_check)

    sub.add_parser("smoke", help="Composio search + scrape smoke test").set_defaults(func=_smoke)

    # TODO later phases: research, verify, score, analyze, site
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
