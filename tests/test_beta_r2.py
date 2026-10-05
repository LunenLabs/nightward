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


# ---- R2-OPS-03 (D11): one writer per store -------------------------------------

def test_second_writer_fails_fast_while_the_store_is_locked(approved_app):
    from nightward.core.lock import store_lock
    tmp_path, tw = approved_app
    before = (tw / "pending" / "replicas.received.json").read_bytes()
    with store_lock(tw, "nightward run"):
        for args in (("run", "."), ("approve", "--all"), ("reject", "replicas")):
            r = cli(*args, "--dir", str(tw), cwd=tmp_path)
            assert r.returncode == 2, (args, r.stdout, r.stderr)
            assert "another nightward process" in r.stderr and "nightward run" in r.stderr
        with pytest.raises(NightwardError, match="another nightward process"):
            mcp_server.run_tool(str(tmp_path), str(tw))
    assert (tw / "pending" / "replicas.received.json").read_bytes() == before
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert not (tw / ".lock").exists()


def test_direct_plugin_flush_refuses_a_locked_store(approved_app):
    from nightward.core.lock import store_lock
    tmp_path, tw = approved_app
    write(tmp_path / "app.py", APP.replace('"dev": 1', '"dev": 0'))
    with store_lock(tw, "nightward run"):
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "--nightward-record", "--nightward-dir", str(tw)],
                           cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode != 0
    assert "another nightward process" in r.stdout + r.stderr
    got = json.loads((tw / "pending" / "replicas.received.json").read_text("utf-8"))
    assert got["payload"]["dev"] == 1


def test_stale_lock_of_a_dead_process_is_taken_over(approved_app):
    tmp_path, tw = approved_app
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True)
    import socket
    write(tw / ".lock", json.dumps({"pid": int(dead.stdout), "host": socket.gethostname(),
                                    "command": "nightward run", "since": "earlier"}))
    r = cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tw / ".lock").exists()


PARAMS = ('import os, pytest\n'
          'ENV = os.environ.get("TOXENV", "py")\n'
          '@pytest.mark.parametrize("i", range(200))\n'
          'def test_c(behavior, i):\n'
          '    behavior(f"cfg.{i:03d}", {"i": i, "py": ENV if i == 7 else "any"}, group="g")\n')


def test_concurrent_runs_never_report_clobbered_removals(tmp_path):
    write(tmp_path / "test_c.py", PARAMS)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    procs = [subprocess.Popen([sys.executable, "-m", "nightward", "run", ".", "--dir", str(tw)],
                              cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env={**os.environ, "TOXENV": env}, text=True,
                              encoding="utf-8", errors="replace")
             for env in ("py", "py312")]
    outs = [(*p.communicate(timeout=300), p.returncode) for p in procs]
    for out, err, code in outs:
        assert "REMOVED" not in out
        assert code in (0, 1) or "another nightward process" in err, err
    assert len(list((tw / "pending").glob("*.json"))) == 200
