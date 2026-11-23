import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent import golden as G
from agent.schema import GoldenField, GoldenLabel

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("label_script", ROOT / "scripts" / "label.py")
label = importlib.util.module_from_spec(spec)
spec.loader.exec_module(label)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def gf(v, url="https://docs.example.com/x", q="quote"):
    return GoldenField(value=v, url=url, quote=q)


def mk(app_id=2, labeller="human", **over):
    f = {"auth_methods": gf(["oauth2"]), "credential_access": gf("self_serve_free"),
         "production_gate": gf("none"), "api_type": gf(["rest"]),
         "mcp_official": gf("no_found"), "main_blocker_type": gf("none")}
    f.update(over)
    return GoldenLabel(app_id=app_id, app="A", labeller=labeller, labelled_at=NOW, fields=f)


def test_ids_match_sample_md():
    text = (ROOT / "data/golden/_sample.md").read_text()
    gold_sec, hold_sec = text.split("## Holdout")
    gold_sec = gold_sec.split("## Golden set")[1]
    rows = lambda s: tuple(int(m) for m in re.findall(r"^\| (\d+) \|", s, re.M))
    assert sorted(rows(gold_sec)) == sorted(G.GOLDEN_IDS) and len(G.GOLDEN_IDS) == 20
    assert sorted(rows(hold_sec.split("## ")[0])) == sorted(G.HOLDOUT_IDS)


def test_load_labels_confirmed_vs_drafts(tmp_path):
    (tmp_path / "drafts").mkdir()
    (tmp_path / "002.json").write_text(mk(2).model_dump_json())
    (tmp_path / "010.json").write_text(mk(10, labeller="draft:x").model_dump_json())  # misplaced
    (tmp_path / "drafts" / "011.json").write_text(mk(11, labeller="draft:claude").model_dump_json())
    assert set(G.load_labels(golden_dir=tmp_path)) == {2}
    assert set(G.load_labels(confirmed_only=False, golden_dir=tmp_path)) == {11}


def test_label_fields_and_parse():
    assert label.ALLOWED["main_blocker_type"] == ("technical", "commercial", "access", "documentation", "none")
    assert label.parse_value("auth_methods", "oauth2, api_key") == ["api_key", "oauth2"]
    assert label.parse_value("auth_methods", "bogus") is None
    assert label.parse_value("mcp_official", "yes") == "yes"
    assert label.parse_value("mcp_official", "maybe") is None


def scripted(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_build_label_fresh_save_roundtrip(tmp_path):
    from agent.registry import get_app
    app = get_app(2)
    u = "https://docs.example.com/a"
    ans = [
        "", "oauth2, nope", "oauth2", u, "q1",       # auth (invalid value retried)
        "", "self_serve_free", u, "q2",
        "u", u, "no gate info",                        # production_gate unknown
        "", "rest", u, "q4",
        "a", "unclear docs",                           # mcp ambiguous
        "", "none", "bad-url", u, "q6",
    ]
    lab, log = label.build_label(app, "tester", scripted(ans), lambda s: None)
    assert lab.fields["production_gate"].value == "unknown"
    assert lab.fields["production_gate"].quote == label.UNKNOWN_QUOTE
    assert lab.fields["mcp_official"].status == "ambiguous"
    assert (log["fields_edited"], log["fields_ambiguous"], log["fields_accepted"]) == (5, 1, 0)
    p = label.save(lab, log, golden_dir=tmp_path)
    assert p == tmp_path / "002.json"
    assert GoldenLabel.model_validate(json.loads(p.read_text())).labeller == "tester"
    entry = json.loads((tmp_path / "_review_log.jsonl").read_text().splitlines()[0])
    assert entry["app_id"] == 2 and entry["from_draft"] is False
    with pytest.raises(FileExistsError):
        label.save(lab, log, golden_dir=tmp_path)


def test_from_draft_accept_edit_ambiguous(tmp_path):
    from agent.registry import get_app
    draft = mk(2, labeller="draft:m")
    u = "https://docs.example.com/new"
    ans = ["",                              # accept auth
           "e", "paid_plan_required", "", "",  # edit credential (keep url/quote)
           "a", "note here",                # production_gate ambiguous
           "", "", ""]                      # accept rest
    lab, log = label.build_label(get_app(2), "h", scripted(ans), lambda s: None, draft=draft)
    assert (log["fields_accepted"], log["fields_edited"], log["fields_ambiguous"]) == (4, 1, 1)
    assert lab.fields["credential_access"].value == "paid_plan_required"
    assert lab.fields["credential_access"].url == "https://docs.example.com/x"
    assert log["from_draft"] is True


def test_save_rejects_draft_labeller(tmp_path):
    with pytest.raises(ValueError):
        label.save(mk(labeller="draft:x"), {}, golden_dir=tmp_path)


def test_accept_all_requires_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(G, "GOLDEN_DIR", tmp_path)
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "002.json").write_text(mk(2, labeller="draft:m").model_dump_json())
    assert label.main(["2", "--accept-all-drafts", "--labeller", "t"]) == 2
    assert not (tmp_path / "002.json").exists()
    assert label.main(["2", "--accept-all-drafts", "--i-reviewed-these", "--labeller", "t"]) == 0
    assert "WARNING" in capsys.readouterr().out
    saved = json.loads((tmp_path / "002.json").read_text())
    assert saved["labeller"] == "t"
    assert json.loads((tmp_path / "_review_log.jsonl").read_text())["mode"] == "accept_all_drafts"


# ------------------------------------------------------------ edge cases (review)
from agent.registry import get_app  # noqa: E402

U = "https://docs.example.com/a"


def no_input(prompt):
    raise AssertionError(f"must not prompt: {prompt!r}")


@pytest.mark.parametrize("raw,expected", [
    (" rest ,graphql ", ["graphql", "rest"]),
    ("REST, GraphQL", ["graphql", "rest"]),
    ("rest,", ["rest"]),
    ("rest, rest", None),          # duplicate
    ("", None), (" , ", None),
    ("unknown", None),             # golden unknown only via 'u'
    ("rest, webhooks", None),      # not an api_type
])
def test_parse_set_field(raw, expected):
    assert label.parse_value("api_type", raw) == expected


def test_parse_scalar_field():
    assert label.parse_value("credential_access", "  Paid_Plan_Required ") == "paid_plan_required"
    assert label.parse_value("credential_access", "paid") is None
    assert label.parse_value("production_gate", "unknown") is None
    assert label.parse_value("production_gate", "none, oauth_app_review") is None


def test_scalar_invalid_reprompts():
    seen = []
    v = label._ask_value("mcp_official", scripted(["maybe", "", "yes"]), seen.append)
    assert v == "yes" and sum("invalid" in s for s in seen) == 2


def test_unknown_set_field_is_schema_valid(tmp_path):
    ans = ["u", "not-a-url", U, "", "searched auth docs"]
    gfld, action = label.prompt_field("auth_methods", scripted(ans), lambda s: None)
    assert gfld.value == ["unknown"] and gfld.url == U and gfld.note == "searched auth docs"
    assert action == "edited" and G.field_value_error("auth_methods", gfld) is None
    lab = mk(2, auth_methods=gfld)
    label.save(lab, {}, golden_dir=tmp_path)
    assert set(G.load_labels(ids=[2], golden_dir=tmp_path)) == {2}


def test_invalid_draft_value_cannot_be_accepted():
    bad = GoldenField(value=["oauth2", "webhook_secret"], url=U, quote="q")
    seen = []
    # Enter is refused, then edit: the bad value is not offered as default
    ans = ["", "e", "", "oauth2", "", ""]
    gfld, action = label.prompt_field("auth_methods", scripted(ans), seen.append, draft=bad)
    assert action == "edited" and gfld.value == ["oauth2"]
    assert any("DRAFT INVALID" in s for s in seen)
    assert not any("Enter keeps ['oauth2', 'webhook_secret']" in s for s in seen)


def test_save_validates_values_before_writing(tmp_path):
    with pytest.raises(G.InvalidLabel):
        label.save(mk(credential_access=gf("free")), {"x": 1}, golden_dir=tmp_path)
    assert not (tmp_path / "002.json").exists()
    assert not (tmp_path / "_review_log.jsonl").exists()


def test_save_force_overwrites_and_logs_twice(tmp_path):
    label.save(mk(), {"n": 1}, golden_dir=tmp_path)
    label.save(mk(credential_access=gf("contact_sales")), {"n": 2}, golden_dir=tmp_path, force=True)
    saved = json.loads((tmp_path / "002.json").read_text())
    assert saved["fields"]["credential_access"]["value"] == "contact_sales"
    assert len((tmp_path / "_review_log.jsonl").read_text().splitlines()) == 2


def test_main_refuses_existing_before_prompting(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(G, "GOLDEN_DIR", tmp_path)
    (tmp_path / "002.json").write_text(mk(2).model_dump_json())
    monkeypatch.setattr("builtins.input", no_input)
    assert label.main(["2", "--labeller", "t"]) == 1
    assert "exists" in capsys.readouterr().out


def test_main_rejects_draft_for_other_app(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "GOLDEN_DIR", tmp_path)
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "002.json").write_text(mk(10, labeller="draft:m").model_dump_json())
    monkeypatch.setattr("builtins.input", no_input)
    assert label.main(["2", "--from-draft", "--labeller", "t"]) == 2


def test_accept_all_invalid_draft_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "GOLDEN_DIR", tmp_path)
    (tmp_path / "drafts").mkdir()
    d = mk(2, labeller="draft:m", api_type=gf("rest"))  # set field as a string
    (tmp_path / "drafts" / "002.json").write_text(d.model_dump_json())
    assert label.main(["2", "--accept-all-drafts", "--i-reviewed-these", "--labeller", "t"]) == 1
    assert not (tmp_path / "002.json").exists()


def test_accept_all_never_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "GOLDEN_DIR", tmp_path)
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "002.json").write_text(mk(2, labeller="draft:m").model_dump_json())
    monkeypatch.setattr("builtins.input", no_input)
    assert label.main(["2", "--accept-all-drafts", "--labeller", "t"]) == 2  # gate first
    assert label.main(["2", "--accept-all-drafts", "--i-reviewed-these", "--labeller", "t"]) == 0
    log = json.loads((tmp_path / "_review_log.jsonl").read_text())
    assert log["seconds_spent"] == 0.0 and log["from_draft"] is True and "logged_at" in log


def test_review_log_timing(monkeypatch):
    ticks = iter([100.0, 142.46])
    monkeypatch.setattr(label.time, "monotonic", lambda: next(ticks))
    draft = mk(2, labeller="draft:m")
    lab, log = label.build_label(get_app(2), "h", scripted([""] * 6), lambda s: None,
                                 draft=draft, now=NOW)
    assert log["seconds_spent"] == 42.5 and log["fields_accepted"] == 6
    assert log["mode"] == "interactive" and log["logged_at"] == NOW.isoformat()
    assert lab.labelled_at == NOW and lab.labeller == "h"


def test_label_tool_never_touches_agent_output(monkeypatch, tmp_path):
    src = (ROOT / "scripts" / "label.py").read_text()
    for banned in ("load_record", "AppRecord", '"pass1"', '"pass2"', '"work"',
                   "data/work", "import accuracy", "agent.accuracy"):
        assert banned not in src, banned
    # runtime: any file opened during a full interactive session is under the golden dir
    import builtins
    monkeypatch.setattr(G, "GOLDEN_DIR", tmp_path)
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "002.json").write_text(mk(2, labeller="draft:m").model_dump_json())
    opened = []
    real_open = builtins.open

    def spy(file, *a, **k):
        opened.append(Path(file).resolve())
        return real_open(file, *a, **k)
    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "read_text",
                        lambda self, *a, **k: (opened.append(self.resolve()),
                                               real_open(self, *a, **k).read())[1])
    monkeypatch.setattr("builtins.input", scripted([""] * 6))
    assert label.main(["2", "--from-draft", "--labeller", "t"]) == 0
    data, tmp = (ROOT / "data").resolve(), tmp_path.resolve()
    under_data = [p for p in opened if data in p.parents]
    assert all(p == data / "apps.json" for p in under_data), under_data  # registry only
    assert tmp / "drafts" / "002.json" in opened                         # draft was shown
    assert (tmp / "002.json").exists()
