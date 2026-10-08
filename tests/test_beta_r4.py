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


# ---- R4-OPS-01 (D24): collection-time exclusion never proves a removal ----------

RENDER_V1 = ('def test_one(behavior):\n'
             '    behavior("render.dev", "dev", group="render")\n'
             '    behavior("render.meta", 1, group="render")\n')


@pytest.fixture
def render(tmp_path):
    write(tmp_path / "test_render.py", RENDER_V1)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    write(tmp_path / "test_render.py",
          'def test_one(behavior):\n    behavior("render.meta", 1, group="render")\n')
    return tmp_path, tmp_path / ".nightward"


@pytest.mark.parametrize("conftest, moved", [
    ('collect_ignore_glob = ["*_integration.py"]\n', "test_render_integration.py"),
    ('def pytest_collection_modifyitems(config, items):\n'
     '    items[:] = [i for i in items if "slow" not in i.keywords]\n', "test_render_slow.py"),
])
def test_capture_moved_into_an_excluded_test_is_kept(render, conftest, moved):
    tmp_path, tw = render
    write(tmp_path / "conftest.py", conftest)
    write(tmp_path / moved, 'import pytest\n@pytest.mark.slow\ndef test_dev(behavior):\n'
                            '    behavior("render.dev", "dev", group="render")\n')
    cli("run", ".", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert "render.dev" in baseline_names(tw), r.stdout
    assert "excluded at collection" in r.stdout


def test_legacy_behavior_is_never_bulk_removed(render):
    # D24: no recorded test, no bulk proof - even after a clean run.
    tmp_path, tw = render
    f = tw / "baseline" / "render.dev.approved.json"
    data = json.loads(f.read_text("utf-8"))
    data.pop("source")
    f.write_text(json.dumps(data), encoding="utf-8")
    cli("run", ".", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert "render.dev" in baseline_names(tw), r.stdout
    assert "approve --remove" in r.stdout


def test_ignore_in_addopts_is_not_a_clean_run(tmp_path):
    write(tmp_path / "test_unit.py", 'def test_u(behavior):\n    behavior("unit", 1)\n')
    write(tmp_path / "test_e2e.py", 'def test_e(behavior):\n    behavior("e2e", 2)\n')
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    write(tmp_path / "pytest.ini", "[pytest]\naddopts = --ignore=test_e2e.py\n")
    cli("run", ".", cwd=tmp_path)
    meta = json.loads((tmp_path / ".nightward" / "run_meta.json").read_text("utf-8"))
    assert meta["clean"] is False and "excluded at collection" in meta["clean_doubt"]


# ---- R4-DATA-02 (D25): explicit human removal is cheap --------------------------

PARTS = ('import os, pytest\n'
         'def test_parts(behavior):\n'
         '    for d in range(3):\n'
         '        behavior(f"daily.d{d}", d, group="daily")\n'
         '        if not os.environ.get("DROP"):\n'
         '            behavior(f"hourly.d{d}", d, group="hourly")\n'
         'def test_spark():\n'
         '    pytest.importorskip("no_such_module_pyspark")\n')


@pytest.fixture
def parts(tmp_path):
    write(tmp_path / "test_parts.py", PARTS)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    cli("run", ".", cwd=tmp_path, env={"DROP": "1"})       # 3 REMOVED, 1 skipped
    return tmp_path, tmp_path / ".nightward"


def test_remove_group_drops_exactly_its_removed_behaviors(parts):
    tmp_path, tw = parts
    cli("review", "--group", "hourly", cwd=tmp_path)
    r = cli("approve", "--remove-group", "hourly", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    for d in range(3):
        assert f"removed hourly.d{d}" in r.stdout
    assert baseline_names(tw) == ["daily.d0", "daily.d1", "daily.d2"]


def test_remove_names_refuses_anything_not_removed(parts):
    tmp_path, tw = parts
    cli("review", cwd=tmp_path)
    r = cli("approve", "--remove", "hourly.d0", "daily.d1", cwd=tmp_path)
    assert r.returncode == 2 and "daily.d1" in r.stderr
    assert len(baseline_names(tw)) == 6
    r = cli("approve", "--remove", "hourly.d0", "hourly.d2", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "hourly.d0" not in baseline_names(tw) and "hourly.d1" in baseline_names(tw)
    # the "kept" hint names the cheap override
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert "approve --remove" in r.stdout
