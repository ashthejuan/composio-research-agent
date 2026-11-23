from datetime import datetime, timezone

import pytest

from agent import accuracy as A
from agent import attribution as T
from agent.schema import (
    AppRecord, Blocker, Claim, Evidence, GoldenField, GoldenLabel, LoopResult, RunInfo, dump_record,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
URL = "https://docs.example.com/page"
GOOD = dict(auth_methods=["oauth2"], credential_access="self_serve_free", production_gate="none",
            api_type=["rest"], mcp_official="no_found", main_blocker_type="none")


def gf(v):
    return GoldenField(value=v, url=URL, quote="q")


def label(app_id):
    return GoldenLabel(app_id=app_id, app="A", labeller="human", labelled_at=NOW,
                       fields={f: gf(v) for f, v in GOOD.items()})


def record(app_id, pass_, ledgers=None, **vals):
    base = AppRecord.unknown(app_id, "A", pass_, RunInfo(model="m", started_at=NOW))
    upd = {}
    ev = [Evidence(url=URL, quote="q", source_tier="official_docs")]
    for k, v in {**GOOD, **vals}.items():
        unk = v in ("unknown", ["unknown"])
        if k == "main_blocker_type":
            upd["main_blocker"] = Claim(value=Blocker(text="t", type=v), evidence=[] if unk else ev)
        else:
            upd[k] = Claim(value=v, evidence=[] if unk else ev)
    for k, loops in (ledgers or {}).items():
        upd["main_blocker" if k == "main_blocker_type" else k].verification.loops = loops
    return base.model_copy(update={"claims": base.claims.model_copy(update=upd)})


@pytest.fixture
def env(tmp_path):
    gd, b, a = tmp_path / "g", tmp_path / "b", tmp_path / "a"
    for d in (gd, b, a):
        d.mkdir()

    def put(app_id, before, after):
        (gd / f"{app_id:03d}.json").write_text(label(app_id).model_dump_json())
        dump_record(before, b / f"{app_id:03d}-a.json")
        dump_record(after, a / f"{app_id:03d}-a.json")

    def go(ids, **kw):
        return T.attribute("pass1", "pass2", before_dir=b, after_dir=a, label_dir=gd, ids=ids,
                           out=tmp_path / "attr.json", **kw)
    return put, go, tmp_path


L = LoopResult


def test_transitions_and_crediting(env):
    put, go, tmp = env
    ledger = {
        "credential_access": [L(loop="L1", result="pass"), L(loop="L3", result="fail"),
                              L(loop="L5", result="contradicted"),
                              L(loop="L6", result="changed", detail='"x" -> "self_serve_free"')],
        "production_gate": [L(loop="L6", result="changed")],  # regressed below
        "api_type": [L(loop="L5", result="supported")],
    }
    put(1, record(1, "pass1", credential_access="paid_plan_required", production_gate="none",
                  mcp_official="unknown", api_type=["graphql"], main_blocker_type="technical",
                  auth_methods=["unknown"]),
        record(1, "pass2", ledger, production_gate="oauth_app_review", mcp_official="unknown",
               api_type=["graphql"], main_blocker_type="unknown", auth_methods=["unknown"]))
    res = go((1,))
    by = {c["field"]: c for c in res["claims"]}
    cr = by["credential_access"]
    assert cr["transition"] == "fixed" and cr["responsible_loops"] == ["L3", "L5", "L6"]
    assert cr["primary_trigger"] == "L3"
    assert by["production_gate"]["transition"] == "regressed"
    assert by["production_gate"]["responsible_loops"] == ["L6"]
    assert by["mcp_official"]["transition"] == "unchanged_abstained"
    assert by["api_type"]["transition"] == "unchanged_wrong" and by["api_type"]["responsible_loops"] == []
    assert by["main_blocker_type"]["transition"] == "wrong_to_abstained"
    assert by["auth_methods"]["transition"] == "unchanged_abstained"
    assert res["transitions"]["fixed"] == 1 and len(res["regressions"]) == 1
    e = res["errors_caught_per_loop"]
    assert e["credited"]["L3"] == e["credited"]["L5"] == e["credited"]["L6"] == 1 and e["credited"]["L1"] == 0
    assert e["primary_trigger"]["L3"] == 1 and e["fixed_total"] == 1 and e["fixed_without_ledger"] == 0


def test_fixed_without_ledger_not_invented(env):
    put, go, _ = env
    put(1, record(1, "pass1", api_type=["graphql"]), record(1, "pass2"))
    res = go((1,))
    c = next(c for c in res["claims"] if c["field"] == "api_type")
    assert c["transition"] == "fixed" and c["responsible_loops"] == [] and c["primary_trigger"] is None
    assert res["errors_caught_per_loop"]["primary_trigger"]["unattributed"] == 1
    assert res["errors_caught_per_loop"]["fixed_without_ledger"] == 1


def test_abstained_to_wrong_and_excluded():
    assert T.transition("abstained", "wrong") == "abstained_to_wrong"
    assert T.transition("abstained", "correct") == "fixed"
    assert T.transition("partial", "correct") == "fixed"
    assert T.transition("correct", "partial") == "regressed"
    assert T.transition("excluded", "correct") == "excluded"


def test_rules_hash_mismatch_raises(env):
    put, go, _ = env
    put(1, record(1, "pass1"), record(1, "pass2"))
    b = A.score("pass1", ids=(1,), label_dir=env[2] / "g", records_dir=env[2] / "b")
    a = A.score("pass2", ids=(1,), label_dir=env[2] / "g", records_dir=env[2] / "a")
    a.rules_sha256 = "0" * 64
    with pytest.raises(T.RulesMismatch):
        T.build(b, a, env[2] / "a")


def test_claim_outcomes_complete_and_totals_unchanged(env):
    put, _, tmp = env
    lab = label(2)
    lab.fields["api_type"] = GoldenField(status="ambiguous", note="n")
    (tmp / "g" / "002.json").write_text(lab.model_dump_json())
    dump_record(record(2, "pass1", mcp_official="unknown"), tmp / "b" / "002-a.json")
    r = A.score("pass1", ids=(2,), label_dir=tmp / "g", records_dir=tmp / "b")
    assert len(r.claim_outcomes) == r.overall["evaluated"] + r.overall["excluded"] == 6
    assert {c["outcome"] for c in r.claim_outcomes} == {"correct", "abstained", "excluded"}
    assert all(set(c) == {"app_id", "app", "field", "outcome", "golden", "agent"} for c in r.claim_outcomes)
    assert r.overall["correct"] == 4 and r.overall["abstained"] == 1 and r.overall["excluded"] == 1
    import hashlib
    assert r.rules_sha256 == hashlib.sha256(A.RULES_PATH.read_bytes()).hexdigest()
