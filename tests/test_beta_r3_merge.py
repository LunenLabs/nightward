"""Beta round 3 merge: where the core (D18/D19/D21), ai (D20/D22, MCP store pin)
and ux (--group) fixes meet, frozen as tests.

Each test pins a seam the separate branches could not see. A failure here is a
real defect - fix the code, do not weaken the test.
"""
import os
import subprocess
import sys

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute


def cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env={**os.environ})


def two_group_store(tw):
    """g1.a and g2.b both CHANGED, with a fresh report and nothing reviewed yet."""
    store = Store(tw)
    store.ensure()
    for name, group in (("g1.a", "g1"), ("g2.b", "g2")):
        store.write_pending(Behavior(name=name, payload=1, group=group, source="t.py::h"))
        store.approve(name)
    store.clear_pending()
    for name, group in (("g1.a", "g1"), ("g2.b", "g2")):
        store.write_pending(Behavior(name=name, payload=2, group=group, source="t.py::h"))
    store.write_run_meta({"completed": ["t.py::h"]})
    recompute(store)
    return store


def test_approve_group_needs_that_group_reviewed(tmp_path):
    # --group goes through D19's reviewed-token check: a scoped review of g2
    # licenses g2 only.
    store = two_group_store(tmp_path / ".tw")
    assert cli("review", "--group", "g2", "--dir", str(store.root), cwd=tmp_path).returncode == 0
    r = cli("approve", "--group", "g1", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 2 and "was not shown in your last review" in r.stderr, r.stderr
    assert store.load_baseline()["g1.a"].payload == 1
    r = cli("approve", "--group", "g2", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert store.load_baseline()["g2.b"].payload == 2


def test_approve_group_refuses_a_stale_report(tmp_path):
    store = two_group_store(tmp_path / ".tw")
    assert cli("review", "--dir", str(store.root), cwd=tmp_path).returncode == 0
    # a `git pull` brings a teammate's approval of g1.a after the report
    store.write_pending(Behavior(name="g1.a", payload=3, group="g1", source="t.py::h"))
    store.approve("g1.a")
    store.write_pending(Behavior(name="g1.a", payload=2, group="g1", source="t.py::h"))
    r = cli("approve", "--group", "g1", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 2 and "changed since the last report" in r.stderr, r.stderr
    assert store.load_baseline()["g1.a"].payload == 3


def test_init_and_run_name_a_rule_that_ignores_the_judge_ledger(tmp_path):
    # The ledger is one file per ruling under <store>/judge/ (D22): a rule that
    # ignores it keeps rulings off CI and teammates' clones.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".nightward/judge/\n", encoding="utf-8")
    (tmp_path / "test_a.py").write_text('def test_a(behavior):\n    behavior("a", 1)\n',
                                        encoding="utf-8")
    r = cli("init", cwd=tmp_path)
    assert r.returncode == 0
    assert "judge ledger (.nightward/judge/) is git-ignored" in r.stderr, r.stderr
    r = cli("run", ".", cwd=tmp_path)
    assert "judge ledger" in r.stderr, r.stderr


def test_a_server_pinned_in_a_subdirectory_still_refuses_a_second_store(tmp_path, monkeypatch):
    # `nightward mcp` pins its store (R3-LLM-07) as an absolute path; the
    # subdirectory guard (R3-OPS-02) must still see that it is the wrong one.
    from nightward import mcp_server
    from nightward.errors import NightwardError
    (tmp_path / ".nightward").mkdir()
    (tmp_path / "tests").mkdir()
    monkeypatch.chdir(tmp_path / "tests")
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    monkeypatch.setattr(mcp_server, "_server_dir", None)
    mcp_server.configure(dir=".nightward")
    for call in (mcp_server.status_tool, mcp_server.run_tool):
        with pytest.raises(NightwardError, match="start the MCP server in the project root"):
            call()
    assert not (tmp_path / "tests" / ".nightward").exists()
