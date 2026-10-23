"""[tool.nightward] path: the committed capture suite.

From the PetTracker dogfood: the captures lived in one 3-second directory, but
`run`, `gate --run` and MCP `nightward_run` all ran "." - a suite of over 15
minutes - so the agent loop and `gate --run` were unusable.
"""
import os
import subprocess
import sys

import pytest

from nightward import mcp_server
from nightward.config import capture_path
from nightward.errors import NightwardError

CAPTURE = 'def test_a(behavior):\n    behavior("a", {"v": 1})\n'
SLOW = 'def test_slow():\n    raise SystemExit("the full suite ran")\n'


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def project(tmp_path, setting: str | None = 'path = "tests/nightward"'):
    """A project whose captures live in tests/nightward, next to a suite that
    must not run (it fails the run if it does)."""
    (tmp_path / "tests" / "nightward").mkdir(parents=True)
    (tmp_path / "tests" / "nightward" / "test_capture.py").write_text(CAPTURE, encoding="utf-8")
    (tmp_path / "tests" / "test_slow.py").write_text(SLOW, encoding="utf-8")
    if setting is not None:
        (tmp_path / "pyproject.toml").write_text(f"[tool.nightward]\n{setting}\n",
                                                 encoding="utf-8")
    assert cli("init", cwd=tmp_path).returncode == 0
    return tmp_path


def test_unset_means_no_committed_path(tmp_path):
    assert capture_path(project(tmp_path, setting=None) / ".nightward") is None


@pytest.mark.parametrize("bad", ["path = 3", 'path = ""', 'path = ["a", "b"]'])
def test_a_wrong_value_names_the_file(tmp_path, bad):
    (tmp_path / "pyproject.toml").write_text(f"[tool.nightward]\n{bad}\n", encoding="utf-8")
    with pytest.raises(NightwardError, match=r"pyproject\.toml.*path"):
        capture_path(tmp_path / ".nightward")


def test_the_path_resolves_from_the_pyproject_not_the_cwd(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path / "tests")
    assert os.path.samefile(capture_path(tmp_path / ".nightward"),
                            tmp_path / "tests" / "nightward")


def test_run_without_a_path_runs_the_committed_suite(tmp_path):
    project(tmp_path)
    r = cli("run", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "the full suite ran" not in r.stdout + r.stderr
    assert (tmp_path / ".nightward" / "pending" / "a.received.json").exists()


def test_pytest_args_without_a_path_still_use_the_committed_suite(tmp_path):
    project(tmp_path)
    r = cli("run", "--", "-k", "test_a", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "the full suite ran" not in r.stdout + r.stderr


def test_an_explicit_path_wins(tmp_path):
    project(tmp_path)
    r = cli("run", "tests", cwd=tmp_path)
    assert "the full suite ran" in r.stdout + r.stderr


def test_no_setting_still_runs_dot(tmp_path):
    project(tmp_path, setting=None)
    r = cli("run", cwd=tmp_path)
    assert "the full suite ran" in r.stdout + r.stderr


def test_gate_run_uses_the_committed_suite_and_path_overrides_it(tmp_path):
    project(tmp_path)
    cli("run", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    r = cli("gate", "--run", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    r = cli("gate", "--run", "--path", "tests", cwd=tmp_path)
    assert "the full suite ran" in r.stdout + r.stderr


def test_gate_path_without_run_is_an_error(tmp_path):
    project(tmp_path)
    r = cli("gate", "--path", "tests", cwd=tmp_path)
    assert r.returncode == 2 and "--run" in r.stderr


def test_mcp_run_without_a_path_uses_the_committed_suite(tmp_path, monkeypatch):
    project(tmp_path)
    monkeypatch.chdir(tmp_path)
    out = mcp_server.run_tool(dir=str(tmp_path / ".nightward"))
    assert out["boundary"] == "breached", out
    assert [c["name"] for c in out["changes"]] == ["a"]
    assert os.path.samefile(out["path"], tmp_path / "tests" / "nightward")
