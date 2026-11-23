import asyncio
import json
import sys
import types
from datetime import datetime, timezone

import pytest

from agent import cli, orchestrator
from agent.registry import load_apps
from agent.schema import AppRecord, LoopResult, RunInfo, dump_record, load_record


def _rec(app, pass_="pass1", **run):
    now = datetime.now(timezone.utc)
    return AppRecord.unknown(app.id, app.name, pass_, RunInfo(model="m", started_at=now, **run))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "RETRY_DELAYS", (0, 0))
    monkeypatch.setattr("agent.tools.base.get_tools", lambda backend=None: ("s", "c", "k"))
    fake = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "agent.verify", fake)
    import agent
    monkeypatch.setattr(agent, "verify", fake, raising=False)
    apps = load_apps()[:3]
    src = tmp_path / "pass1"
    for a in apps:
        r = _rec(a)
        r.errors = ["note-from-pass1"]
        dump_record(r, orchestrator.record_path(src, a))
    return fake, tmp_path, apps, src


def _run(apps, tmp, src, label="pass2", **kw):
    return asyncio.run(orchestrator.run_verify_batch(
        "pass1", label, apps=apps, src_dir=src, out_dir=tmp / "work", reports_dir=tmp / "rep",
        concurrency=2, **kw))


def _ok_verify(calls=None):
    async def v(record, app, tools):
        if calls is not None:
            calls.append(app.id)
        assert tools == ("s", "c", "k")
        c = record.claims.description
        c.verification.loops.append(LoopResult(loop="L1", result="pass"))
        record.pass_ = "pass2"
        record.run = record.run.model_copy(update={"verifier_model": "vm", "verifier_tokens_in": 7,
                                                   "verifier_tokens_out": 3, "l6_tool_calls": 2})
        return record
    return v


def test_success_manifest_and_pass2_records(env):
    fake, tmp, apps, src = env
    fake.verify_app = _ok_verify()
    m = _run(apps, tmp, src, label="dryrun3")  # non-pass2 label still writes pass2 records
    assert m.ok == 3 and not m.failed and m.record_pass == "pass2"
    files = sorted((tmp / "work").glob("*.json"))
    assert len(files) == 3 and all(load_record(f).pass_ == "pass2" for f in files)
    t = m.totals
    assert t["verifier_tokens_in"] == 21 and t["verifier_tokens_out"] == 9 and t["l6_tool_calls"] == 6
    assert t["verifier_models"] == ["vm"] and t["ledger_entries"] == 3
    data = json.loads((tmp / "rep" / "run_dryrun3.json").read_text())
    assert data["record_pass"] == "pass2" and data["apps_total"] == 3


def test_retry_then_success(env):
    fake, tmp, apps, src = env
    n = {"c": 0}
    ok = _ok_verify()

    async def flaky(record, app, tools):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("boom")
        return await ok(record, app, tools)

    fake.verify_app = flaky
    m = _run(apps[:1], tmp, src)
    assert m.ok == 1 and not m.failed and n["c"] == 2


def test_permanent_failure_copies_pass1_record(env):
    fake, tmp, apps, src = env
    ok = _ok_verify()

    async def v(record, app, tools):
        if app.id == apps[1].id:
            raise RuntimeError("down")
        return await ok(record, app, tools)

    fake.verify_app = v
    m = _run(apps, tmp, src)
    assert [f.id for f in m.failed] == [apps[1].id] and m.ok == 2
    bad = load_record(orchestrator.record_path(tmp / "work", apps[1]))
    assert bad.pass_ == "pass2" and bad.errors[0] == "note-from-pass1"
    assert bad.errors[-1].startswith("verify failed: RuntimeError")
    assert orchestrator.is_failed(bad)
    # failed apps are retried on the next run; good ones are skipped
    calls = []
    fake.verify_app = _ok_verify(calls)
    m2 = _run(apps, tmp, src)
    assert calls == [apps[1].id] and m2.skipped == 2 and m2.ok == 1


def test_skip_existing_and_no_skip(env):
    fake, tmp, apps, src = env
    fake.verify_app = _ok_verify()
    _run(apps, tmp, src)
    calls = []
    fake.verify_app = _ok_verify(calls)
    m = _run(apps, tmp, src)
    assert calls == [] and m.skipped == 3
    _run(apps, tmp, src, skip_existing=False)
    assert len(calls) == 3


def test_missing_source_record_raises(env):
    fake, tmp, apps, src = env
    fake.verify_app = _ok_verify()
    with pytest.raises(FileNotFoundError):
        _run(load_apps()[5:6], tmp, src)


def test_freeze_pass2(tmp_path):
    src, dest = tmp_path / "w", tmp_path / "pass2"
    for a in load_apps()[:100]:
        dump_record(_rec(a, "pass2"), orchestrator.record_path(src, a))
    assert orchestrator.freeze("pass2", src=src, dest=dest) == dest
    bad = tmp_path / "w2"
    for a in load_apps()[:100]:
        dump_record(_rec(a, "pass1"), orchestrator.record_path(bad, a))
    with pytest.raises(orchestrator.FreezeError):
        orchestrator.freeze("pass2", src=bad, dest=tmp_path / "x")


def test_cli_parsing():
    p = cli.build_parser()
    a = p.parse_args(["verify", "--from", "pass1", "--to", "dryrun3", "--ids", "1-10", "--concurrency", "4",
                      "--backend", "direct", "--no-skip"])
    assert (a.from_, a.to, a.ids, a.concurrency, a.backend, a.no_skip) == ("pass1", "dryrun3", "1-10", 4, "direct", True)
    assert p.parse_args(["verify", "--all"]).to == "pass2"
    with pytest.raises(SystemExit):
        p.parse_args(["verify"])
    a = p.parse_args(["loopstats", "--pass", "pass2", "--records-dir", "d"])
    assert a.pass_ == "pass2" and a.records_dir == "d"
    a = p.parse_args(["attribute", "--before", "pass1", "--after", "pass2"])
    assert (a.before, a.after) == ("pass1", "pass2")
