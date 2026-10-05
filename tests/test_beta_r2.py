"""Beta round 2: gate-integrity defects, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import subprocess
import sys

import pytest

from nightward import mcp_server
from nightward.errors import NightwardError
from nightward.runner import execute_run


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def status_json(tmp_path, tw):
    return json.loads(cli("status", "--json", "--dir", str(tw), cwd=tmp_path).stdout)


APP = 'def replicas(env):\n    return {"dev": 1, "prod": 4}[env]\n'
TEST_APP = ('from app import replicas\n'
            'def test_replicas(behavior):\n'
            '    behavior("replicas", {e: replicas(e) for e in ["dev", "prod"]}, group="p")\n')


@pytest.fixture
def approved_app(tmp_path):
    write(tmp_path / "app.py", APP)
    write(tmp_path / "test_app.py", TEST_APP)
    tw = tmp_path / ".tw"
    assert cli("run", ".", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0
    return tmp_path, tw


# ---- R2-OPS-01 (D12): an aborted run never leaves the old green report ---------

@pytest.mark.parametrize("break_it", [
    lambda p: write(p / "app.py", "import yaml_missing_dep\n" + APP),     # exit 2
    lambda p: (p / "test_app.py").unlink(),                              # exit 5
    lambda p: write(p / "conftest.py", "import missing_plugin_dep\n"),   # exit 4
])
def test_aborted_run_invalidates_the_report(approved_app, break_it):
    tmp_path, tw = approved_app
    break_it(tmp_path)
    r = cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2
    assert "report was invalidated" in r.stderr
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode != 0
    assert status_json(tmp_path, tw)["boundary"] == "unknown"
    assert mcp_server.status_tool(str(tw))["boundary"] == "unknown"
    assert (tw / "pending" / "replicas.received.json").exists()   # capture untouched


def test_timed_out_run_invalidates_the_report(approved_app):
    tmp_path, tw = approved_app
    write(tmp_path / "test_slow.py", "import time\ndef test_slow():\n    time.sleep(30)\n")
    with pytest.raises(NightwardError, match="timed out"):
        execute_run(str(tmp_path), str(tw), capture_output=True, timeout=3)
    assert not (tw / "report.json").exists()


def test_run_on_a_vanished_path_invalidates_the_report(approved_app):
    tmp_path, tw = approved_app
    r = cli("run", "tests_renamed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2 and "does not exist" in r.stderr
    assert status_json(tmp_path, tw)["boundary"] == "unknown"
