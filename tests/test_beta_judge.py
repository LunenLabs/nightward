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


def test_mcp_run_reuses_the_judge_of_the_last_run(tmp_path, monkeypatch):
    from nightward import mcp_server
    monkeypatch.delenv("NIGHTWARD_JUDGE", raising=False)
    path, dir_, _store = _approved_project(tmp_path)
    monkeypatch.setenv("REPLY", REWORDED)
    assert execute_run(path, dir_, judge_spec="persona:editor")["report"]["boundary"] == "intact"

    out = mcp_server.run_tool(path, dir_)
    assert out["boundary"] == "intact"                 # same verdict as the CLI
    assert out["judge"]["spec"] == "persona:editor"
    meta = json.loads((tmp_path / ".nightward" / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["judge"] == "persona:editor"           # not erased by the agent's run


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
