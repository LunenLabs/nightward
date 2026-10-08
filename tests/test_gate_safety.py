"""Gate-safety cases: paths that used to end green while the boundary was lost.

Each test here failed on the code before its fix. A failure is a real defect.
"""
import json
import subprocess
import sys

import pytest

from nightward import mcp_server
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.core.diff import compare
from nightward.errors import NightwardError
from nightward.runner import execute_run

TWO = ('def test_a(behavior):\n    behavior("a", {"v": 1})\n'
       'def test_b(behavior):\n    behavior("b", {"v": 2})\n')


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


@pytest.fixture
def approved_pair(tmp_path):
    """A project with behaviors a and b captured and approved."""
    write(tmp_path / "test_s.py", TWO)
    tw = tmp_path / ".tw"
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    assert sorted(p.name for p in (tw / "baseline").iterdir()) == [
        "a.approved.json", "b.approved.json"]
    return tmp_path, tw


# ---- approve --all must not shrink the boundary on false REMOVED ------------

def test_approve_all_keeps_removed_by_default(approved_pair):
    tmp_path, tw = approved_pair
    r = cli("run", "test_s.py::test_a", "--dir", str(tw), cwd=tmp_path)  # partial path
    assert r.returncode == 0, r.stderr

    r = cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tw / "baseline" / "b.approved.json").exists()
    assert "--include-removed" in r.stdout
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 1


def test_include_removed_approves_removals(approved_pair):
    tmp_path, tw = approved_pair
    write(tmp_path / "test_s.py", 'def test_a(behavior):\n    behavior("a", {"v": 1})\n')
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)

    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tw / "baseline" / "b.approved.json").exists()
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0


def test_include_removed_refused_after_incomplete_run(approved_pair):
    tmp_path, tw = approved_pair
    write(tmp_path / "test_s.py",
          'import pytest\n'
          'def test_a(behavior):\n    behavior("a", {"v": 1})\n'
          '@pytest.mark.skip(reason="off")\n'
          'def test_b(behavior):\n    behavior("b", {"v": 2})\n')
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)

    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2
    assert "skipped" in r.stderr
    assert (tw / "baseline" / "b.approved.json").exists()


# ---- group moves -------------------------------------------------------------

def test_group_move_is_changed():
    old = Behavior("x", {"v": 1}, group="billing")
    new = Behavior("x", {"v": 1}, group="loyalty")
    (change,) = compare({"x": old}, {"x": new})
    assert change.kind == "CHANGED"
    assert "billing" in change.diff_text and "loyalty" in change.diff_text


# ---- stale report --------------------------------------------------------------

def test_report_stale_after_baseline_change(approved_pair):
    tmp_path, tw = approved_pair
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0
    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["generated_at"]

    (tw / "baseline" / "b.approved.json").unlink()   # baseline moved under the report

    r = cli("gate", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 1
    assert "stale" in r.stdout
    status = json.loads(cli("status", "--json", "--dir", str(tw), cwd=tmp_path).stdout)
    assert status["stale"] is True


def test_fresh_report_not_stale(approved_pair):
    tmp_path, tw = approved_pair
    status = json.loads(cli("status", "--json", "--dir", str(tw), cwd=tmp_path).stdout)
    assert status["stale"] is False


def test_status_tool_flags_stale_report(tmp_path):
    tw = tmp_path / ".nightward"
    write(tmp_path / "test_s.py", 'def test_a(behavior):\n    behavior("a", {"v": 1})\n')
    mcp_server.run_tool(str(tmp_path / "test_s.py"), str(tw))
    assert mcp_server.status_tool(str(tw))["stale"] is False
    Store(tw).approve("a")          # baseline changes after the report was written
    assert mcp_server.status_tool(str(tw))["stale"] is True


# ---- xdist ---------------------------------------------------------------------

# Under xdist each worker records only its share and the controller records
# nothing, so pending would come out empty (mass false REMOVED).
def test_execute_run_neutralizes_xdist_addopts(tmp_path):
    pytest.importorskip("xdist")
    write(tmp_path / "test_s.py", TWO)
    write(tmp_path / "pytest.ini", "[pytest]\naddopts = -n 2\n")
    result = execute_run(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"))
    assert result["report"]["counts"]["new"] == 2


def test_record_with_xdist_is_a_usage_error(tmp_path):
    pytest.importorskip("xdist")
    write(tmp_path / "test_s.py", TWO)
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "test_s.py", "-n", "2", "--nightward-record",
         "--nightward-dir", str(tmp_path / ".nightward")],
        cwd=str(tmp_path), capture_output=True, text=True,
    )
    assert r.returncode == pytest.ExitCode.USAGE_ERROR, r.stdout + r.stderr
    assert "xdist" in (r.stdout + r.stderr)


# ---- runner: timeout and failure output ------------------------------------------

# A hung suite must not hang the caller (the MCP server blocks on this call).
def test_execute_run_timeout(tmp_path):
    write(tmp_path / "test_slow.py", "import time\ndef test_slow(behavior):\n    time.sleep(30)\n")
    with pytest.raises(NightwardError, match="timed out"):
        execute_run(str(tmp_path / "test_slow.py"), str(tmp_path / ".nightward"),
                    capture_output=True, timeout=3)


# With output captured (MCP), the agent still needs to know WHY tests failed.
def test_run_tool_surfaces_failure_output(tmp_path):
    write(tmp_path / "test_f.py",
          'def test_f(behavior):\n    behavior("f", 1)\n    assert False, "DISTINCTIVE_FAILURE"\n')
    out = mcp_server.run_tool(str(tmp_path / "test_f.py"), str(tmp_path / ".nightward"))
    assert "DISTINCTIVE_FAILURE" in out["warnings"]["pytest_output_tail"]


# ---- packaging -----------------------------------------------------------------

def test_version_single_source():
    # pyproject reads nightward.__version__ dynamically; installed metadata must agree.
    from importlib.metadata import version

    import nightward
    assert version("nightward") == nightward.__version__
