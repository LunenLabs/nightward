"""Beta round 5c: polish items from the final retest, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


APP_JS = Path(__file__).parents[1] / "src" / "nightward" / "view" / "assets" / "app.js"


def node_eval(js_expr):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    runner = (
        "const vm = require('vm'), fs = require('fs');"
        "const ctx = {fetch: () => new Promise(() => {}), console};"
        "vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
        "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2], ctx)));"
    )
    r = subprocess.run([node, "-e", runner, str(APP_JS), js_expr], capture_output=True,
                       text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- R5-POL-01: the "not reviewed" refusal says what actually happened ----------

TOTAL = ('import os\n'
         'def test_t(behavior):\n'
         '    behavior("total", int(os.environ.get("T", "1")), group="g")\n')
GUESS = ("agent", "teammate", "CI")


@pytest.fixture
def total(tmp_path):
    write(tmp_path / "test_t.py", TOTAL)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    return tmp_path


def test_never_reviewed_says_only_review_marks(total):
    r = cli("approve", "--all", cwd=total)
    assert r.returncode == 2
    assert "not reviewed" in r.stderr and "nightward review" in r.stderr
    assert "`run` lists names only" in r.stderr
    assert not any(g in r.stderr for g in GUESS), r.stderr


def test_own_rerun_is_named_not_blamed_on_others(total):
    cli("review", cwd=total)
    cli("run", ".", cwd=total, env={"T": "2"})
    r = cli("approve", "--all", cwd=total)
    assert r.returncode == 2
    assert "changed since your review" in r.stderr
    assert "taken by `nightward run` at " in r.stderr, r.stderr
    assert not any(g in r.stderr for g in GUESS), r.stderr
    assert "nightward review total" in r.stderr


def test_an_agent_rerun_is_named_as_the_agent(total, monkeypatch):
    cli("review", cwd=total)
    monkeypatch.chdir(total)
    monkeypatch.setenv("T", "2")
    from nightward import mcp_server
    mcp_server.run_tool(".")
    r = cli("reject", "total", cwd=total)
    assert r.returncode == 2
    assert "taken by `nightward_run (MCP)`" in r.stderr, r.stderr


def test_a_name_left_out_of_a_scoped_review_is_not_reviewed(tmp_path):
    write(tmp_path / "test_t.py", 'def test_t(behavior):\n    behavior("a", 1)\n'
                                  '    behavior("b", 2)\n')
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("review", "a", cwd=tmp_path)
    r = cli("approve", "a", "b", cwd=tmp_path)
    assert r.returncode == 2
    assert "b: not reviewed yet" in r.stderr and "changed" not in r.stderr, r.stderr


def test_bare_pytest_capture_is_named(total):
    cli("review", cwd=total)
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                    "--nightward-record"], cwd=str(total), capture_output=True,
                   env={**os.environ, "T": "2"})
    cli("report", cwd=total)
    r = cli("approve", "--all", cwd=total)
    assert "taken by `pytest --nightward-record`" in r.stderr, r.stderr


def test_run_meta_records_who_captured(total):
    meta = json.loads((total / ".nightward" / "run_meta.json").read_text("utf-8"))
    assert meta["captured_by"] == "nightward run" and meta["captured_at"]


# ---- R5-POL-02: a new judge ruling is part of what to commit -------------------

REPLY = ('import os\n'
         'def test_r(behavior):\n'
         '    behavior("reply", os.environ.get("R", "Your refund is approved."), group="g",\n'
         '             semantic=True)\n')


@pytest.fixture
def replies(tmp_path):
    write(tmp_path / "pyproject.toml", '[tool.nightward]\njudge = "persona:editor"\n')
    write(tmp_path / "test_r.py", REPLY)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    return tmp_path


def test_run_names_new_ruling_files_to_commit(replies):
    r = cli("run", ".", cwd=replies, env={"R": "your refund is approved"})
    (ruling,) = (replies / ".nightward" / "judge").glob("*.json")
    out = r.stdout + r.stderr
    assert ruling.name in out and "commit .nightward/judge/" in out, out
    # nothing new on the next run: no note
    r = cli("run", ".", cwd=replies, env={"R": "your refund is approved"})
    assert "commit .nightward/judge/" not in r.stdout + r.stderr


def test_mcp_warns_about_new_ruling_files(replies, monkeypatch):
    monkeypatch.chdir(replies)
    monkeypatch.setenv("R", "your refund is approved")
    from nightward import mcp_server
    w = mcp_server.run_tool(".")["warnings"]
    (ruling,) = (replies / ".nightward" / "judge").glob("*.json")
    assert w["judge_added"] == [ruling.name]
    assert mcp_server.run_tool(".")["warnings"]["judge_added"] == []

