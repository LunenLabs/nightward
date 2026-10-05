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
