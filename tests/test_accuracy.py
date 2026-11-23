import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent import accuracy as A
from agent.schema import (
    AppRecord, Blocker, Claim, Evidence, GoldenField, GoldenLabel, RunInfo, dump_record,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
URL = "https://docs.example.com/page"


def run():
    return RunInfo(model="m", started_at=NOW, backend="t")


def gf(v):
    return GoldenField(value=v, url=URL, quote="q")


def amb():
    return GoldenField(status="ambiguous", note="n")


def label(app_id=2, **over):
    f = {"auth_methods": gf(["oauth2"]), "credential_access": gf("self_serve_free"),
         "production_gate": gf("none"), "api_type": gf(["rest"]),
         "mcp_official": gf("no_found"), "main_blocker_type": gf("none")}
    f.update(over)
    return GoldenLabel(app_id=app_id, app="A", labeller="human", labelled_at=NOW, fields=f)


def claim(v):
    ev = [] if v in ("unknown", ["unknown"]) else [Evidence(url=URL, quote="q", source_tier="official_docs")]
    return Claim(value=v, evidence=ev)


def record(app_id=2, **vals):
    base = AppRecord.unknown(app_id, "A", "pass1", run())
    cl = base.claims
    upd = {}
    for k, v in vals.items():
        if k == "main_blocker_type":
            ev = [] if v == "unknown" else [Evidence(url=URL, quote="q", source_tier="official_docs")]
            upd["main_blocker"] = Claim(value=Blocker(text="t", type=v), evidence=ev)
        else:
            upd[k] = claim(v)
    return base.model_copy(update={"claims": cl.model_copy(update=upd)})


PERFECT = dict(auth_methods=["oauth2"], credential_access="self_serve_free", production_gate="none",
               api_type=["rest"], mcp_official="no_found", main_blocker_type="none")


@pytest.fixture
def env(tmp_path):
    gd, rd = tmp_path / "golden", tmp_path / "recs"
    gd.mkdir(); rd.mkdir()

    def put(lab, rec):
        (gd / f"{lab.app_id:03d}.json").write_text(lab.model_dump_json())
        dump_record(rec, rd / f"{lab.app_id:03d}-a.json")

    def go(ids=(2,), **kw):
        return A.score("pass1", ids=ids, label_dir=gd, records_dir=rd, **kw)
    return gd, rd, put, go


def test_all_correct(env):
    _, _, put, go = env
    put(label(), record(**PERFECT))
    r = go()
    assert r.overall["correct"] == 6 and r.overall["accuracy"] == 100.0
    assert not r.incomplete and r.misses == []


def test_wrong_abstained_partial_excluded(env):
    _, _, put, go = env
    put(label(mcp_official=amb(), api_type=gf(["rest", "graphql"])),
        record(**{**PERFECT, "credential_access": "paid_plan_required", "production_gate": "unknown",
                  "mcp_official": "yes"}))
    r = go()
    o = r.overall
    assert (o["evaluated"], o["excluded"], o["correct"], o["wrong"], o["abstained"], o["partial"]) == (5, 1, 2, 2, 1, 1)
    assert o["accuracy"] == 40.0
    by = {m["field"]: m for m in r.misses}
    assert by["credential_access"]["outcome"] == "wrong"
    assert by["production_gate"]["outcome"] == "abstained"
    assert by["api_type"]["outcome"] == "partial"
    assert by["api_type"]["tag"] is None and by["api_type"]["golden_url"] == URL
    assert by["credential_access"]["agent_evidence_urls"] == [URL]
    assert [e["field"] for e in r.excluded] == ["mcp_official"]


def test_auth_class_vs_strict(env):
    _, _, put, go = env
    put(label(auth_methods=gf(["bearer_token"])), record(**{**PERFECT, "auth_methods": ["api_key"]}))
    r = go()
    assert r.per_field["auth_methods"]["correct"] == 1
    assert r.strict_auth_accuracy == 0.0
    # api_key + bearer is still the same class as api_key alone
    put(label(auth_methods=gf(["api_key"])), record(**{**PERFECT, "auth_methods": ["api_key", "bearer_token"]}))
    assert go().per_field["auth_methods"]["correct"] == 1


def test_auth_partial_and_oauth_not_api_key(env):
    _, _, put, go = env
    put(label(auth_methods=gf(["oauth2", "api_key"])), record(**{**PERFECT, "auth_methods": ["oauth2"]}))
    r = go()
    assert r.per_field["auth_methods"]["wrong"] == 1 and r.per_field["auth_methods"]["partial"] == 1


def test_golden_unknown_semantics(env):
    _, _, put, go = env
    put(label(production_gate=gf("unknown"), auth_methods=gf(["unknown"])),
        record(**{**PERFECT, "production_gate": "unknown", "auth_methods": ["oauth2"]}))
    r = go()
    assert r.per_field["production_gate"]["correct"] == 1  # abstain is right
    assert r.per_field["auth_methods"]["wrong"] == 1       # concrete value is wrong
    assert r.per_field["auth_methods"]["abstained"] == 0


def test_main_blocker_uses_type(env):
    _, _, put, go = env
    put(label(main_blocker_type=gf("commercial")), record(**{**PERFECT, "main_blocker_type": "commercial"}))
    assert go().per_field["main_blocker_type"]["correct"] == 1


def test_missing_label_and_record_flagged(env):
    _, rd, put, go = env
    put(label(), record(**PERFECT))
    r = go(ids=(2, 10))
    assert r.incomplete and r.labels_missing == [10] and r.n_labels_missing == 1 and r.n_apps == 1
    (rd / "002-a.json").unlink()
    r = go()
    assert r.incomplete and r.records_missing == [2]


def test_rules_hash_recorded(env):
    _, _, put, go = env
    put(label(), record(**PERFECT))
    expected = hashlib.sha256(A.RULES_PATH.read_bytes()).hexdigest()
    r = go()
    assert r.rules_sha256 == expected and r.provisional is False
    assert json.loads(r.to_json())["pass"] == "pass1"


def test_drafts_excluded_unless_included(env):
    gd, rd, put, go = env
    (gd / "drafts").mkdir()
    d = label(2).model_copy(update={"labeller": "draft:m"})
    (gd / "drafts" / "002.json").write_text(d.model_dump_json())
    dump_record(record(**PERFECT), rd / "002-a.json")
    r = go()
    assert r.n_apps == 0 and r.labels_missing == [2] and r.incomplete
    r = go(include_drafts=True)
    assert r.n_apps == 1 and r.provisional is True and r.overall["correct"] == 6


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """CLI pointed at temp golden/reports dirs; never touches real data/."""
    from agent import cli, golden
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(golden, "GOLDEN_DIR", tmp_path / "g")
    monkeypatch.setattr(A, "REPORTS_DIR", tmp_path / "reports")
    rd = tmp_path / "recs"; rd.mkdir()
    return cli, rd, tmp_path / "reports"


def test_cli_drafts_default_path(cli_env, capsys):
    cli, rd, reports = cli_env
    rc = cli.main(["score", "--pass", "pass1", "--include-drafts", "--records-dir", str(rd)])
    assert rc == 0
    out = reports / "accuracy_pass1.PROVISIONAL_drafts.json"
    assert out.exists() and json.loads(out.read_text())["provisional"] is True
    assert "PROVISIONAL" in capsys.readouterr().out
    assert not (reports / "accuracy_pass1.json").exists()
    assert cli.main(["score", "--include-drafts", "--out", "data/reports/accuracy_pass1.json"]) == 2


def test_cli_drafts_cannot_write_any_official_path(cli_env):
    cli, rd, reports = cli_env
    # a different pass's official name, or any name lacking PROVISIONAL, is refused
    for out in ("accuracy_pass2.json", "accuracy_final.json", "x/report.json"):
        assert cli.main(["score", "--include-drafts", "--records-dir", str(rd), "--out", out]) == 2
        assert not Path(out).exists()
    assert not reports.exists() or not list(reports.glob("*.json"))


def test_cli_official_default_path(cli_env):
    cli, rd, reports = cli_env
    assert cli.main(["score", "--pass", "pass2", "--records-dir", str(rd)]) == 0
    rep = json.loads((reports / "accuracy_pass2.json").read_text())
    assert rep["provisional"] is False and rep["incomplete"] is True  # no labels at all


def test_write_report_guard(tmp_path):
    rep = A.AccuracyReport(rules_sha256="x", pass_name="pass1", scored_at="t", provisional=True)
    with pytest.raises(ValueError):
        A.write_report(rep, tmp_path / "accuracy_pass1.json")
    assert not (tmp_path / "accuracy_pass1.json").exists()
    assert A.write_report(rep, tmp_path / "a.PROVISIONAL.json").exists()
    official = rep.model_copy(update={"provisional": False})
    assert A.write_report(official, tmp_path / "accuracy_pass1.json").exists()


# ------------------------------------------------------------ edge cases (review)
from agent import golden as G  # noqa: E402

SC = A.score_claim


@pytest.mark.parametrize("field,golden,agent,expected", [
    # golden unknown: agent unknown is correct, any concrete value is wrong (never partial)
    ("api_type", ["unknown"], ["unknown"], ("correct", False, True)),
    ("api_type", ["unknown"], ["rest"], ("wrong", False, False)),
    ("main_blocker_type", "unknown", "unknown", ("correct", False, True)),
    ("main_blocker_type", "unknown", "access", ("wrong", False, False)),
    # agent unknown where golden known -> abstained (not wrong, not partial)
    ("api_type", ["rest", "graphql"], ["unknown"], ("abstained", False, False)),
    ("auth_methods", ["oauth2"], ["unknown"], ("abstained", False, False)),
    ("main_blocker_type", "commercial", "unknown", ("abstained", False, False)),
    ("credential_access", "contact_sales", "unknown", ("abstained", False, False)),
    # set fields: disjoint = wrong, superset/subset overlap = wrong + partial
    ("api_type", ["rest"], ["graphql"], ("wrong", False, False)),
    ("api_type", ["rest"], ["rest", "graphql"], ("wrong", True, False)),
    ("api_type", ["graphql", "rest"], ["rest", "graphql"], ("correct", False, True)),
    # auth class level: api_key == bearer_token; strict reported separately
    ("auth_methods", ["api_key", "oauth2"], ["bearer_token", "oauth2"], ("correct", False, False)),
    ("auth_methods", ["api_key", "oauth2"], ["bearer_token"], ("wrong", True, False)),
    ("auth_methods", ["api_key"], ["api_key", "oauth2"], ("wrong", True, False)),
    ("auth_methods", ["basic"], ["api_key"], ("wrong", False, False)),
    ("auth_methods", ["oauth2", "api_key"], ["api_key", "oauth2"], ("correct", False, True)),
    # scalar fields: exact match, never partial
    ("production_gate", "oauth_app_review", "marketplace_listing", ("wrong", False, False)),
    ("mcp_official", "yes", "yes", ("correct", False, True)),
])
def test_score_claim_table(field, golden, agent, expected):
    assert SC(field, gf(golden), agent) == expected


def test_ambiguous_excluded_whatever_agent_says():
    for a in (["unknown"], ["oauth2"]):
        assert SC("auth_methods", amb(), a)[0] == "excluded"


def test_all_excluded_accuracy_none_and_strict_skips_ambiguous(env):
    _, _, put, go = env
    put(label(**{f: amb() for f in PERFECT}), record(**PERFECT))
    r = go()
    o = r.overall
    assert (o["claims"], o["evaluated"], o["excluded"], o["accuracy"]) == (6, 0, 6, None)
    assert r.strict_auth_accuracy is None and len(r.excluded) == 6 and r.misses == []


def test_per_app_and_per_field_math(env):
    _, _, put, go = env
    put(label(2), record(2, **PERFECT))
    put(label(10, auth_methods=amb()),
        record(10, **{**PERFECT, "credential_access": "unknown", "api_type": ["rest", "soap"]}))
    r = go(ids=(2, 10))
    assert r.per_app["2"]["accuracy"] == 100.0 and r.per_app["2"]["label_source"] == "confirmed"
    a10 = r.per_app["10"]
    assert (a10["evaluated"], a10["correct"], a10["abstained"], a10["wrong"], a10["partial"]) == (5, 3, 1, 1, 1)
    assert a10["fields"]["auth_methods"] == "excluded" and a10["accuracy"] == 60.0
    o = r.overall
    assert (o["claims"], o["evaluated"], o["correct"]) == (12, 11, 9)
    assert o["accuracy"] == round(100 * 9 / 11, 2) and o["abstain_rate"] == round(100 / 11, 2)
    assert r.per_field["auth_methods"]["evaluated"] == 1 and r.strict_auth_accuracy == 100.0
    assert r.per_field["api_type"]["partial"] == 1
    assert sum(d["correct"] for d in r.per_field.values()) == o["correct"]


def test_record_glob_unambiguous(env):
    _, rd, put, go = env
    put(label(2), record(2, **PERFECT))
    (rd / "_frozen.json").write_text("{}")         # snapshot manifest, not a record
    (rd / "run_pass1.json").write_text("{}")
    (rd / "002-hubspot.json.bak").write_text("{}")
    (rd / "0021-x.json").write_text("{}")          # not id 2
    assert go().overall["correct"] == 6
    dump_record(record(2, **PERFECT), rd / "002-hubspot-old.json")
    with pytest.raises(A.AmbiguousRecord):
        go()


def test_record_app_id_mismatch_rejected(env):
    _, rd, put, go = env
    put(label(2), record(2, **PERFECT))
    dump_record(record(3, **PERFECT), rd / "002-a.json")  # wrong content under id 2
    with pytest.raises(ValueError):
        go()


def test_default_records_dir_ignores_non_record_json(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "DATA_DIR", tmp_path)
    (tmp_path / "pass2").mkdir()
    (tmp_path / "pass2" / "_frozen.json").write_text("{}")
    assert A.default_records_dir("pass2") == tmp_path / "work" / "pass2"
    (tmp_path / "pass2" / "002-a.json").write_text("{}")
    assert A.default_records_dir("pass2") == tmp_path / "pass2"


def test_scoring_reads_read_only_files(env):
    import os
    import stat
    gd, rd, put, go = env
    put(label(2), record(2, **PERFECT))
    for f in (*gd.glob("*.json"), *rd.glob("*.json")):
        os.chmod(f, stat.S_IRUSR)
    os.chmod(rd, stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert go().overall["correct"] == 6
    finally:
        os.chmod(rd, stat.S_IRWXU)


def test_invalid_golden_values_rejected(env):
    gd, rd, put, go = env
    bad = {
        "credential_access": gf("free"),                  # not an enum value
        "mcp_official": gf(["yes"]),                      # scalar given as list
        "api_type": gf("rest"),                           # set given as string
        "auth_methods": gf(["unknown", "oauth2"]),        # unknown mixed in
    }
    for f, g in bad.items():
        put(label(2, **{f: g}), record(2, **PERFECT))
        with pytest.raises(G.InvalidLabel):
            go()


def test_label_file_app_id_must_match_filename(env):
    gd, rd, put, go = env
    (gd / "002.json").write_text(label(10).model_dump_json())
    dump_record(record(2, **PERFECT), rd / "002-a.json")
    with pytest.raises(G.InvalidLabel):
        go()


def test_drafts_override_confirmed_only_when_provisional(env):
    gd, rd, put, go = env
    put(label(2), record(2, **PERFECT))
    (gd / "drafts").mkdir()
    d = label(2, credential_access=gf("contact_sales")).model_copy(update={"labeller": "draft:m"})
    (gd / "drafts" / "002.json").write_text(d.model_dump_json())
    r = go()
    assert r.overall["correct"] == 6 and r.per_app["2"]["label_source"] == "confirmed"
    r = go(include_drafts=True)
    assert r.provisional and r.per_app["2"]["label_source"] == "draft"
    assert r.overall["correct"] == 5


def test_rules_hash_follows_rules_file(env, tmp_path):
    _, _, put, go = env
    put(label(), record(**PERFECT))
    alt = tmp_path / "rules.md"
    alt.write_text("other rules")
    assert go(rules_path=alt).rules_sha256 == hashlib.sha256(b"other rules").hexdigest()
    assert go().rules_sha256 == A.rules_sha256() != go(rules_path=alt).rules_sha256


def test_same_rules_across_passes(env):
    _, _, put, go = env
    put(label(), record(**PERFECT))
    gd, rd = env[0], env[1]
    r1 = A.score("pass1", ids=(2,), label_dir=gd, records_dir=rd)
    r2 = A.score("pass2", ids=(2,), label_dir=gd, records_dir=rd)
    assert r1.rules_sha256 == r2.rules_sha256 and r1.overall == r2.overall


def test_missing_labels_and_records_both_listed(env):
    _, rd, put, go = env
    put(label(2), record(2, **PERFECT))
    put(label(10), record(10, **PERFECT))
    (rd / "010-a.json").unlink()
    r = go(ids=(2, 10, 11))
    assert r.labels_missing == [11] and r.records_missing == [10] and r.n_apps == 1
    assert r.incomplete and r.overall["claims"] == 6
