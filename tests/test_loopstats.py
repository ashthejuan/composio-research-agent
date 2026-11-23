from datetime import datetime, timezone

from agent import loopstats
from agent.schema import AppRecord, LoopResult, RunInfo, dump_record

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def rec(app_id=1):
    r = AppRecord.unknown(app_id, "A", "pass2", RunInfo(model="m", started_at=NOW))
    return AppRecord.model_validate_json(r.model_dump_json())  # all_unknown() shares Claim objects; unshare


def test_counts_and_status(tmp_path):
    r = rec()
    a = r.claims.auth_methods.verification
    a.status = "supported"
    a.loops += [LoopResult(loop="L1", result="pass"), LoopResult(loop="L2", result="fail"),
                LoopResult(loop="L2", result="fail"),  # two entries, one claim
                LoopResult(loop="L5", result="contradicted"),
                LoopResult(loop="L6", result="changed", detail='["a"] -> ["b"]')]
    m = r.claims.api_type.verification
    m.status = "insufficient_evidence"
    m.loops += [LoopResult(loop="L5", result="partially_supported"), LoopResult(loop="L6", result="set_unknown"),
                LoopResult(loop="L3", result="error")]
    r.claims.description.verification.loops.append(LoopResult(loop="L1", result="fail"))  # non-golden field
    dump_record(r, tmp_path / "001-a.json")
    st = loopstats.run("pass2", records_dir=tmp_path, out=tmp_path / "out" / "s.json")
    L = st["overall"]["loops"]
    assert L["L1"] == {"claims_checked": 1, "flagged": 0, "values_changed": 0, "errors": 0}
    assert L["L2"]["claims_checked"] == 1 and L["L2"]["flagged"] == 1
    assert L["L3"]["errors"] == 1 and L["L3"]["flagged"] == 0
    assert L["L5"]["claims_checked"] == 2 and L["L5"]["flagged"] == 2
    assert L["L6"]["values_changed"] == 2
    assert st["per_field"]["auth_methods"]["loops"]["L6"]["values_changed"] == 1
    assert st["per_field"]["api_type"]["status_distribution"]["insufficient_evidence"] == 1
    assert st["overall"]["status_distribution"]["pending"] == 4
    assert st["overall"]["claims"] == 6
    # description is not an evaluated field: only in all_claims
    assert L["L1"]["flagged"] == 0 and st["all_claims"]["loops"]["L1"]["flagged"] == 1
    assert (tmp_path / "out" / "s.json").exists()


def test_report_path():
    assert loopstats.report_path("pass2").name == "loop_stats.json"
    assert loopstats.report_path("dry").name == "loop_stats_dry.json"
