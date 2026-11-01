"""The agent sees what moved without taking the human's review.

gate-loop asks the agent to tell an intended change from a regression, which
needs the values. Before, the only way to see them was `nightward review`, which
records that a human saw the diffs (D10/D19) - so a well-meaning agent made the
human's `approve --all` pass unseen. The tools now carry a bounded diff, and
reading it marks nothing.
"""
import json
import os
import subprocess
import sys

from nightward import mcp_server
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute
from nightward.signal import AGENT_DIFF_LINES, status_payload


def changed_store(tmp_path, old, new, name="total", group="billing"):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name=name, payload=old, group=group))
    store.approve(name)
    store.write_pending(Behavior(name=name, payload=new, group=group))
    recompute(store)
    return store


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def test_status_tool_carries_each_changes_diff(tmp_path):
    store = changed_store(tmp_path, {"total": 10}, {"total": 99})
    out = mcp_server.status_tool(str(store.root))
    (change,) = out["changes"]
    assert '-  "total": 10' in change["diff"]
    assert '+  "total": 99' in change["diff"]
    assert change["diff_cut"] == 0


def test_run_tool_carries_the_diff_too(tmp_path):
    (tmp_path / "test_s.py").write_text(
        'def test_a(behavior):\n    behavior("a", {"v": 1}, group="g")\n', encoding="utf-8")
    out = mcp_server.run_tool(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"))
    (change,) = out["changes"]
    assert change["kind"] == "NEW"
    assert '"v": 1' in change["diff"]


def test_reading_the_diff_marks_nothing_reviewed(tmp_path):
    store = changed_store(tmp_path, {"total": 10}, {"total": 99})
    mcp_server.status_tool(str(store.root))
    assert not store.load_reviewed()
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 2
    assert "not reviewed yet" in r.stdout + r.stderr


def test_a_long_diff_is_cut_and_says_how_much(tmp_path):
    store = changed_store(tmp_path, [f"a{i}" for i in range(300)],
                          [f"b{i}" for i in range(300)])
    (change,) = mcp_server.status_tool(str(store.root))["changes"]
    shown = change["diff"].splitlines()
    assert len(shown) == AGENT_DIFF_LINES
    assert change["diff_cut"] > 0
    full = store.load_report()["blast_radius"]["billing"][0]["diff"].splitlines()
    assert len(shown) + change["diff_cut"] == len(full)


def test_the_diffs_share_one_budget(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    for i in range(40):
        store.write_pending(Behavior(name=f"b{i}", payload=list(range(100)), group="g"))
    recompute(store)
    changes = status_payload(store.load_report(), with_diff=True)["changes"]
    lines = sum(len(c["diff"].splitlines()) for c in changes if c["diff"])
    assert lines <= 10 * AGENT_DIFF_LINES
    late = changes[-1]
    assert late["diff"] is None and late["diff_cut"] > 0   # still says something moved


def test_status_json_leaves_diffs_out_unless_asked(tmp_path):
    changed_store(tmp_path, {"total": 10}, {"total": 99})
    plain = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert "diff" not in plain["changes"][0]
    with_diff = json.loads(cli("status", "--json", "--diff", cwd=tmp_path).stdout)
    assert '+  "total": 99' in with_diff["changes"][0]["diff"]
    assert not Store(tmp_path / ".nightward").load_reviewed()


def test_status_json_diff_keeps_hangul(tmp_path):
    changed_store(tmp_path, {"메모": "가"}, {"메모": "나"})
    out = cli("status", "--json", "--diff", cwd=tmp_path).stdout
    assert '"메모": "나"' in json.loads(out)["changes"][0]["diff"]
