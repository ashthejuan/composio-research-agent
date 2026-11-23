import asyncio
import json
import sys
import types
from datetime import datetime, timezone

import pytest

from agent import cli, orchestrator
from agent.registry import load_apps
from agent.schema import AppRecord, RunInfo, load_record


def _rec(app, pass_="pass1"):
    now = datetime.now(timezone.utc)
    r = AppRecord.unknown(app.id, app.name, pass_, RunInfo(model="m", started_at=now, tool_calls=3,
                                                          tokens_in=10, tokens_out=5, cost_usd=0.5))
    return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr("agent.tools.base.get_tools", lambda backend=None: ("s", "c", "k"))
    fake = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "agent.research", fake)
    import agent
    monkeypatch.setattr(agent, "research", fake, raising=False)
    return fake, tmp_path


def _run(apps, tmp_path, **kw):
    return asyncio.run(orchestrator.run_batch(
        apps, out_dir=tmp_path / "work", reports_dir=tmp_path / "rep", concurrency=kw.pop("concurrency", 2), **kw))


def test_concurrency_and_totals(env):
    fake, tmp = env
    apps = load_apps()[:6]
    cur = mx = 0

    async def ra(app, tools, *, pass_="pass1"):
        nonlocal cur, mx
        cur += 1
        mx = max(mx, cur)
        await asyncio.sleep(0.01)
        cur -= 1
        return _rec(app, pass_)

    fake.research_app = ra
    m = _run(apps, tmp, concurrency=2)
    assert mx == 2
    assert m.ok == 6 and not m.failed
    assert m.totals["tool_calls"] == 18 and m.totals["cost_usd"] == 3.0
    data = json.loads((tmp / "rep" / "run_pass1.json").read_text())
    assert data["apps_total"] == 6 and len(data["apps"]) == 6 and data["pass"] == "pass1"
    assert len(list((tmp / "work").glob("*.json"))) == 6


def test_retry_then_success_and_permanent_failure(env):
    fake, tmp = env
    apps = load_apps()[:3]
    calls = {}

    async def ra(app, tools, *, pass_="pass1"):
        calls[app.id] = calls.get(app.id, 0) + 1
        if app.id == apps[0].id and calls[app.id] == 1:
            raise RuntimeError("flaky")
        if app.id == apps[1].id:
            raise ValueError("boom sk-abcdefghijklmnop")
        return _rec(app, pass_)

    fake.research_app = ra
    m = _run(apps, tmp)
    assert calls[apps[0].id] == 2 and calls[apps[1].id] == 2
    assert m.ok == 2 and len(m.failed) == 1 and m.failed[0].id == apps[1].id
    assert "sk-abcdefghijklmnop" not in m.failed[0].error
    rec = load_record(orchestrator.record_path(tmp / "work", apps[1]))
    assert rec.errors[0].startswith("research failed: ValueError")
    assert load_record(orchestrator.record_path(tmp / "work", apps[2])).app_id == apps[2].id


def test_skip_existing(env):
    fake, tmp = env
    apps = load_apps()[:3]
    n = 0

    async def ra(app, tools, *, pass_="pass1"):
        nonlocal n
        n += 1
        return _rec(app, pass_)

    fake.research_app = ra
    _run(apps, tmp)
    assert n == 3
    m = _run(apps, tmp)
    assert n == 3 and m.skipped == 3 and m.ok == 0
    _run(apps, tmp, skip_existing=False)
    assert n == 6


def _fill(d, n=100):
    for a in load_apps()[:n]:
        from agent.schema import dump_record
        dump_record(_rec(a), orchestrator.record_path(d, a))


def test_freeze_ok_and_immutable(tmp_path):
    src, dest = tmp_path / "w", tmp_path / "p"
    _fill(src)
    orchestrator.freeze("pass1", src=src, dest=dest)
    meta = json.loads((dest / "_frozen.json").read_text())
    assert meta["file_count"] == 100 and len(meta["sha256"]) == 100
    with pytest.raises(orchestrator.FreezeError, match="already contains"):
        orchestrator.freeze("pass1", src=src, dest=dest)


def test_freeze_incomplete_and_invalid(tmp_path):
    src = tmp_path / "w"
    _fill(src, 99)
    with pytest.raises(orchestrator.FreezeError, match="expected 100"):
        orchestrator.freeze("pass1", src=src, dest=tmp_path / "p")
    _fill(src)
    (src / "001-bad.json").write_text("{}")
    with pytest.raises(orchestrator.FreezeError, match="invalid record"):
        orchestrator.freeze("pass1", src=src, dest=tmp_path / "p2")
    assert not (tmp_path / "p2").exists()


def test_status(tmp_path):
    _fill(tmp_path / "w", 3)
    st = orchestrator.status("pass1", src=tmp_path / "w")
    assert st["count"] == 3 and st["unknown_by_field"]["docs_url"] == 3


def test_cli_parsing():
    p = cli.build_parser()
    a = p.parse_args(["research", "--ids", "1-10,15", "--backend", "direct", "--concurrency", "3", "--refresh"])
    assert a.ids == "1-10,15" and a.backend == "direct" and a.concurrency == 3 and a.refresh and a.pass_ == "pass1"
    assert [x.id for x in cli.select_apps(a)] == list(range(1, 11)) + [15]
    a = p.parse_args(["research", "--app", "1", "--no-skip"])
    assert len(cli.select_apps(a)) == 1 and a.no_skip
    a = p.parse_args(["research", "--all"])
    assert len(cli.select_apps(a)) == 100
    cat = load_apps()[0].category
    a = p.parse_args(["research", "--category", cat])
    assert len(cli.select_apps(a)) == 10
    with pytest.raises(SystemExit):
        p.parse_args(["research"])
    assert p.parse_args(["freeze", "--pass", "pass1"]).func is cli._freeze
    assert p.parse_args(["status"]).func is cli._status


def test_failed_records_are_retried_on_resume(env):
    fake, tmp = env
    apps = load_apps()[:2]
    state = {"fail": True, "n": 0}

    async def ra(app, tools, *, pass_="pass1"):
        state["n"] += 1
        if state["fail"] and app.id == apps[0].id:
            raise RuntimeError("down")
        return _rec(app, pass_)

    fake.research_app = ra
    m = _run(apps, tmp)
    assert len(m.failed) == 1
    state["fail"], state["n"] = False, 0
    m = _run(apps, tmp)
    assert state["n"] == 1 and m.skipped == 1 and m.ok == 1 and not m.failed
    assert not orchestrator.is_failed(load_record(orchestrator.record_path(tmp / "work", apps[0])))


def test_run_label_writes_record_pass1(env):
    fake, tmp = env
    apps = load_apps()[:1]
    seen = []

    async def ra(app, tools, *, pass_="pass1"):
        seen.append(pass_)
        return _rec(app, pass_)

    fake.research_app = ra
    m = _run(apps, tmp, pass_="dryrun")
    assert seen == ["pass1"] and m.record_pass == "pass1"
    data = json.loads((tmp / "rep" / "run_dryrun.json").read_text())
    assert data["pass"] == "dryrun" and "cost_priced" in data["totals"]


def test_refresh_does_not_leak_env(env, monkeypatch):
    import os
    fake, tmp = env
    monkeypatch.delenv("REFRESH", raising=False)

    async def ra(app, tools, *, pass_="pass1"):
        assert os.environ.get("REFRESH") == "1"
        return _rec(app, pass_)

    fake.research_app = ra
    _run(load_apps()[:1], tmp, refresh=True)
    assert "REFRESH" not in os.environ


def test_freeze_read_only_and_rejects_labels_and_wrong_pass(tmp_path):
    import os
    src = tmp_path / "w"
    _fill(src)
    with pytest.raises(orchestrator.FreezeError, match="run label"):
        orchestrator.freeze("dryrun", src=src, dest=tmp_path / "d")
    with pytest.raises(orchestrator.FreezeError, match="record pass"):
        orchestrator.freeze("pass2", src=src, dest=tmp_path / "p2")
    dest = orchestrator.freeze("pass1", src=src, dest=tmp_path / "p")
    f = next(dest.glob("0*.json"))
    assert not os.access(f, os.W_OK)


def test_cli_bad_ids_exit_code(capsys):
    assert cli.main(["research", "--ids", "1-x"]) == 2


def test_manifest_and_freeze_record_git_dirty(env, monkeypatch):
    fake, tmp = env
    monkeypatch.setattr(orchestrator, "git_commit", lambda: "abc123")
    monkeypatch.setattr(orchestrator, "git_dirty", lambda: True)

    async def ra(app, tools, *, pass_="pass1"):
        return _rec(app, pass_)

    fake.research_app = ra
    _run(load_apps()[:1], tmp)
    data = json.loads((tmp / "rep" / "run_pass1.json").read_text())
    assert data["git_commit"] == "abc123" and data["git_dirty"] is True
    src = tmp / "w"
    _fill(src)
    dest = orchestrator.freeze("pass1", src=src, dest=tmp / "p")
    meta = json.loads((dest / "_frozen.json").read_text())
    assert meta["git_commit"] == "abc123" and meta["git_dirty"] is True


def test_git_dirty_scoped_to_inputs(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout=" M agent/research.py\n")

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    assert orchestrator.git_dirty() is True
    assert seen["cmd"][:3] == ["git", "status", "--porcelain"] and "agent" in seen["cmd"]
