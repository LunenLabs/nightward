"""Beta round 1: the judge on the run path - validated up front, never silent
when unavailable, the same verdict through MCP, and visible to reviewers."""
import json
import subprocess
import sys

import pytest

import nightward.runner as runner_mod
from nightward.core.baseline import Store
from nightward.errors import NightwardError
from nightward.judge import Judge
from nightward.runner import execute_run

TEST_FILE = '''
import os
def test_reply(behavior):
    text = os.environ.get("REPLY", "Your refund of $50 has been approved.")
    behavior("reply.refund", text, group="chat", semantic=True)
'''
REWORDED = "your refund of $50 has been approved!"


def _project(tmp_path):
    (tmp_path / "test_reply.py").write_text(TEST_FILE, encoding="utf-8")
    return str(tmp_path), str(tmp_path / ".nightward")


def _approved_project(tmp_path):
    path, dir_ = _project(tmp_path)
    execute_run(path, dir_)
    store = Store(tmp_path / ".nightward")
    store.approve("reply.refund")
    return path, dir_, store


def cli(*args, cwd, env=None):
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


# --- R1-LLM-06: a typo'd judge spec fails before the suite, and is never saved --


def test_unknown_persona_fails_at_construction(tmp_path):
    with pytest.raises(NightwardError, match="unknown judge persona 'edtior'"):
        Judge("persona:edtior", cache_path=tmp_path / "c.json")


def test_bad_judge_spec_fails_before_pytest_runs(tmp_path, monkeypatch):
    path, dir_ = _project(tmp_path)

    def no_pytest(*a, **k):
        raise AssertionError("pytest must not run with an invalid judge spec")

    monkeypatch.setattr(runner_mod.subprocess, "run", no_pytest)
    with pytest.raises(NightwardError, match="edtior"):
        execute_run(path, dir_, judge_spec="persona:edtior")
    assert not (tmp_path / ".nightward" / "run_meta.json").exists()


def test_cli_typo_judge_does_not_break_approve(tmp_path):
    _project(tmp_path)
    assert cli("run", ".", cwd=tmp_path).returncode == 0
    r = cli("run", ".", "--judge", "persona:edtior", cwd=tmp_path)
    assert r.returncode == 2 and "edtior" in r.stderr
    assert "passed" not in r.stdout          # the suite never ran
    meta = json.loads((tmp_path / ".nightward" / "run_meta.json").read_text(encoding="utf-8"))
    assert "judge" not in meta
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0


# --- R1-LLM-05: an unavailable judge says so (run output, report, status) ----


def test_judge_records_why_it_could_not_rule(tmp_path, monkeypatch):
    import nightward.judge as judge_mod
    from nightward.judge import JudgeUnavailable

    def down(model, old, new):
        raise JudgeUnavailable("ANTHROPIC_API_KEY not set")

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", down)
    judge = Judge("persona:editor", cache_path=tmp_path / "c.json")
    assert judge.equivalent("a", "b", "f1", "f2", name="reply.refund") is None
    assert judge.summary() == {"spec": "persona:editor",
                               "unavailable": "ANTHROPIC_API_KEY not set",
                               "compared_exactly": ["reply.refund"]}


def test_cli_run_warns_when_the_judge_is_unavailable(tmp_path):
    import os
    _project(tmp_path)
    assert cli("run", ".", cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["REPLY"] = REWORDED
    r = cli("run", ".", "--judge", "anthropic:claude-haiku-4-5", cwd=tmp_path, env=env)
    assert "breached" in r.stdout                       # fails closed ...
    assert "judge anthropic:claude-haiku-4-5 unavailable" in r.stderr   # ... loudly
    assert "1 semantic behavior(s) compared exactly" in r.stderr
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert status["judge"]["unavailable"]
    assert status["judge"]["compared_exactly"] == ["reply.refund"]


# --- R1-LLM-03: MCP nightward_run judges like the team's CLI run ---------------


def _pyproject(tmp_path, judge: str) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[tool.nightward]\njudge = "{judge}"\n',
                                             encoding="utf-8")


def test_mcp_run_uses_the_committed_project_judge(tmp_path, monkeypatch):
    # R2-LLM-02 (D14): the agent's judge is the committed project setting.
    from nightward import mcp_server
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    path, dir_, _store = _approved_project(tmp_path)
    _pyproject(tmp_path, "persona:editor")
    monkeypatch.setenv("REPLY", REWORDED)
    out = mcp_server.run_tool(path, dir_)
    assert out["boundary"] == "intact"
    assert out["judge"]["spec"] == "persona:editor"


def test_mcp_never_inherits_a_cli_override_or_env(tmp_path, monkeypatch):
    # R2-LLM-02: one `--judge persona:lenient` demo used to become the agent's judge.
    from nightward import mcp_server
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    path, dir_, _store = _approved_project(tmp_path)
    monkeypatch.setenv("REPLY", "Your refund of $50 has been denied.")
    assert execute_run(path, dir_, judge_spec="persona:lenient")["report"]["boundary"] == "intact"
    monkeypatch.setenv("NIGHTWARD_JUDGE", "persona:lenient")
    out = mcp_server.run_tool(path, dir_)
    assert out["boundary"] == "breached" and out["judge"] is None


def test_mcp_server_judge_is_set_by_the_human(tmp_path, monkeypatch):
    from nightward import mcp_server
    monkeypatch.delenv("NIGHTWARD_JUDGE", raising=False)
    path, dir_, _store = _approved_project(tmp_path)
    monkeypatch.setenv("REPLY", REWORDED)
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    mcp_server.configure(judge="persona:editor")
    assert mcp_server.run_tool(path, dir_)["boundary"] == "intact"


def test_mcp_agent_cannot_choose_the_judge():
    import inspect

    from nightward import mcp_server
    assert "judge" not in str(inspect.signature(mcp_server.run_tool))
    assert "judge" in (mcp_server.run_tool.__doc__ or "")   # but it is told how it works


def test_mcp_configure_rejects_a_bad_judge_at_startup(monkeypatch):
    from nightward import mcp_server
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    with pytest.raises(NightwardError, match="edtior"):
        mcp_server.configure(judge="persona:edtior")


# --- R1-LLM-04: judged-SAME rulings are visible to a reviewer -----------------


def _judged_same_project(tmp_path, monkeypatch):
    monkeypatch.delenv("NIGHTWARD_JUDGE", raising=False)
    path, dir_, store = _approved_project(tmp_path)
    monkeypatch.setenv("REPLY", REWORDED)
    report = execute_run(path, dir_, judge_spec="persona:editor")["report"]
    monkeypatch.delenv("REPLY")
    return report


def test_report_and_status_list_judged_same_with_diff(tmp_path, monkeypatch):
    from nightward.signal import status_payload
    report = _judged_same_project(tmp_path, monkeypatch)
    assert report["boundary"] == "intact"
    [same] = report["judged_same"]
    assert same["name"] == "reply.refund" and same["judge_model"] == "persona:editor"
    assert REWORDED in same["diff"]
    status = status_payload(report)
    assert status["judged_same"] == [{"name": "reply.refund", "group": "chat",
                                      "judge_model": "persona:editor",
                                      "judge_reason": same["judge_reason"]}]


def test_status_changes_carry_judged_different_rulings(tmp_path, monkeypatch):
    from nightward.signal import status_payload
    monkeypatch.delenv("NIGHTWARD_JUDGE", raising=False)
    path, dir_, _store = _approved_project(tmp_path)
    monkeypatch.setenv("REPLY", "Your refund of $500 has been approved.")
    report = execute_run(path, dir_, judge_spec="persona:editor")["report"]
    [change] = status_payload(report)["changes"]
    assert change["judged"] is True and change["judge_model"] == "persona:editor"
    assert change["judge_reason"]


def test_review_shows_judged_same_wording_even_when_intact(tmp_path, monkeypatch):
    _judged_same_project(tmp_path, monkeypatch)
    r = cli("review", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "nothing to review" not in r.stdout
    assert "reply.refund" in r.stdout and "SAME" in r.stdout
    assert REWORDED in r.stdout


def test_ledger_entry_keeps_the_wording_it_ruled_on(tmp_path, monkeypatch):
    _judged_same_project(tmp_path, monkeypatch)
    ledger = json.loads((tmp_path / ".nightward" / "judge_verdicts.json")
                        .read_text(encoding="utf-8"))
    [entry] = ledger.values()
    assert entry["old"] == "Your refund of $50 has been approved."
    assert entry["new"] == REWORDED


def test_dashboard_data_lists_judged_same(tmp_path, monkeypatch):
    from nightward.view import build_site
    _judged_same_project(tmp_path, monkeypatch)
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert [it["name"] for it in data["report"]["judged_same"]] == ["reply.refund"]
    assert "report.judged_same" in (out / "app.js").read_text(encoding="utf-8")


# --- R2-LLM-05 (D14): the semantic flag is part of the approved identity -----


def _flip_pair(old_text, new_text, old_semantic, new_semantic):
    from nightward.core.behavior import Behavior
    return ({"p": Behavior("p", old_text, group="g", semantic=old_semantic)},
            {"p": Behavior("p", new_text, group="g", semantic=new_semantic)})


def test_turning_semantic_on_is_a_change_and_is_not_judged(tmp_path, monkeypatch):
    import nightward.judge as judge_mod
    from nightward.core.diff import CHANGED, compare

    def explode(model, old, new):
        raise AssertionError("an exact baseline must not be judged")

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", explode)
    judge = Judge("persona:lenient", cache_path=tmp_path / "c.json")
    [c] = compare(*_flip_pair("Refunds need approval.", "refunds need approval",
                              False, True), judge=judge)
    assert c.kind == CHANGED and not c.judged
    assert "semantic: False -> True" in c.diff_text


def test_semantic_flip_alone_is_a_change(tmp_path):
    from nightward.core.diff import CHANGED, compare
    for old, new in ((False, True), (True, False)):
        [c] = compare(*_flip_pair("same", "same", old, new))
        assert c.kind == CHANGED and f"semantic: {old} -> {new}" in c.diff_text


def test_judge_runs_only_when_baseline_and_capture_are_semantic(tmp_path):
    from nightward.core.diff import UNCHANGED, compare
    judge = Judge("persona:editor", cache_path=tmp_path / "c.json")
    [c] = compare(*_flip_pair("Approved.", "approved", True, True), judge=judge)
    assert c.kind == UNCHANGED and c.judged


# --- R2-LLM-02 (D14): the judge is a committed project setting ---------------


def test_project_judge_reads_the_nearest_pyproject(tmp_path):
    from nightward.config import project_judge
    _pyproject(tmp_path, "persona:editor")
    (tmp_path / "tests").mkdir()
    assert project_judge(tmp_path / "tests") == "persona:editor"
    assert project_judge(tmp_path) == "persona:editor"


def test_project_judge_none_without_setting(tmp_path):
    from nightward.config import project_judge
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    assert project_judge(tmp_path) is None


@pytest.mark.parametrize("body,match", [
    ('[tool.nightward]\njudge = "persona:edtior"\n', "edtior"),
    ("[tool.nightward]\njudge = 3\n", "provider:model"),
    ("[tool.nightward\n", "pyproject.toml"),
])
def test_project_judge_errors_name_the_file(tmp_path, body, match):
    from nightward.config import project_judge
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    with pytest.raises(NightwardError, match=match) as exc:
        project_judge(tmp_path)
    assert "pyproject.toml" in str(exc.value)


def test_cli_run_uses_project_judge_and_override_is_not_inherited(tmp_path):
    import os
    _project(tmp_path)
    assert cli("run", ".", cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    _pyproject(tmp_path, "persona:editor")
    env = {k: v for k, v in os.environ.items() if k != "NIGHTWARD_JUDGE"}
    env["REPLY"] = REWORDED
    r = cli("run", ".", cwd=tmp_path, env=env)
    assert "intact" in r.stdout and "persona:editor" in r.stdout   # says which judge
    env["REPLY"] = "Your refund of $50 has been denied."
    assert "intact" in cli("run", ".", "--judge", "persona:lenient", cwd=tmp_path,
                           env=env).stdout
    assert "breached" in cli("run", ".", cwd=tmp_path, env=env).stdout   # back to editor
