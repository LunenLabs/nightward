"""Beta round 4: gate-integrity defects, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import subprocess
import sys

import pytest


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def baseline_names(tw):
    return sorted(f.name.split(".approved.json")[0] for f in (tw / "baseline").glob("*.json"))


def status_json(tmp_path):
    return json.loads(cli("status", "--json", cwd=tmp_path).stdout)


# ---- R4-WEB-03, R4-DATA-01 (D23): "not checked" is never "done" ----------------

ETL = ('import os, pytest\n'
       'def test_revenue(behavior):\n'
       '    behavior("revenue", 990 if os.environ.get("BROKEN") else 1000, group="kpi")\n'
       'def test_churn(behavior):\n'
       '    behavior("churn", 0.05, group="kpi")\n'
       '@pytest.mark.slow\n'
       'def test_backfill(behavior):\n'
       '    behavior("backfill_rows", 123456, group="nightly")\n')


@pytest.fixture
def etl(tmp_path):
    write(tmp_path / "pytest.ini", '[pytest]\naddopts = -m "not slow"\nmarkers =\n'
                                   '    slow: nightly only\n')
    write(tmp_path / "test_etl.py", ETL)
    cli("init", cwd=tmp_path)
    cli("run", ".", "--", "-m", "", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    return tmp_path, tmp_path / ".nightward"


def test_deselecting_the_broken_test_is_partial_not_done(etl, monkeypatch):
    # R4-DATA-01: an agent marks the broken test `slow`.
    tmp_path, tw = etl
    write(tmp_path / "test_etl.py", ETL.replace("def test_revenue", "@pytest.mark.slow\n"
                                                                   "def test_revenue"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROKEN", "1")
    from nightward import mcp_server
    r = mcp_server.run_tool(".")
    assert r["boundary"] == "partial" and r["unapproved"] == 0
    assert {n["name"] for n in r["not_run"]} == {"revenue", "backfill_rows"}
    assert mcp_server.status_tool()["boundary"] == "partial"
    g = cli("gate", cwd=tmp_path)
    assert g.returncode == 1 and "partial" in g.stdout and "--allow-not-run" in g.stdout
    assert status_json(tmp_path)["boundary"] == "partial"


def test_gate_allow_not_run_is_an_explicit_opt_in(etl):
    # R4-WEB-03: a deliberately narrowed CI job (addopts deselects nightly tests).
    tmp_path, tw = etl
    r = cli("run", ".", cwd=tmp_path)
    assert "partial" in r.stdout and "not checked" in r.stdout, r.stdout
    assert cli("gate", cwd=tmp_path).returncode == 1
    g = cli("gate", "--allow-not-run", cwd=tmp_path)
    assert g.returncode == 0 and "backfill_rows" in g.stdout
    # a real breach still fails with the opt-in
    cli("run", ".", cwd=tmp_path, env={"BROKEN": "1"})
    assert cli("gate", "--allow-not-run", cwd=tmp_path).returncode == 1
