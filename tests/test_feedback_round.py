"""Feedback round: review --summary, the capture environment, gate --run.

From a user's first real review (a 59 KB diff, a baseline made on Windows
gated elsewhere, a gate that trusted an old run). Each test failed on the code
before its change.
"""
import json
import os
import subprocess
import sys

from nightward.core import summary
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, capture_env
from nightward.pytest_plugin import Recorder
from nightward.runner import recompute
from nightward.signal import status_payload

WINDOWS = {"os": "Windows", "python": "3.12"}
LINUX = {"os": "Linux", "python": "3.12"}


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def store_with(tmp_path, old, new, *, old_env=None, new_env=None, name="evals"):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name=name, payload=old, group="g", env=old_env))
    store.approve(name)
    store.write_pending(Behavior(name=name, payload=new, group="g", env=new_env))
    return store


# ---- review --summary ---------------------------------------------------------


def eval_payloads():
    old = {"cases": [{"id": f"c{i:02d}", "correct": "ERR ImportError"} for i in range(80)],
           "model": "m1"}
    new = {"cases": [{"id": f"c{i:02d}",
                      "correct": "ERR ImportError" if i < 2 else f"answer {i % 41}"}
                     for i in range(80)],
           "model": "m1"}
    return old, new


def test_summary_folds_list_indices_and_counts_values():
    old, new = eval_payloads()
    [path] = summary.changes(old, new)
    assert (path.path, path.changed, path.total) == ("cases[*].correct", 78, 80)
    line = summary.change_line(path)
    assert line.startswith('"ERR ImportError" -> 41 distinct values: ')
    assert "+38 more" in line


def test_summary_single_pair_and_absent_keys():
    [one] = summary.changes({"tax": 2.55}, {"tax": 5.1})
    assert summary.change_line(one) == "2.55 -> 5.1"
    added = summary.changes({"a": 1}, {"a": 1, "b": [1, 2]})
    assert [(s.path, summary.change_line(s)) for s in added] == [("b", "(absent) -> [2 items]")]


def test_summary_keeps_the_difference_in_long_strings_visible():
    head = "x" * 100
    [s] = summary.changes({"t": head + "old tail"}, {"t": head + "new tail"})
    (o, n), = s.pairs
    assert o != n and "old" in o and "new" in n


def test_summary_treats_a_resized_list_as_one_value():
    # An insertion must not read as "every later element changed".
    [s] = summary.changes({"xs": [1, 2, 3]}, {"xs": [0, 1, 2, 3]})
    assert (s.path, summary.change_line(s)) == ("xs", "[3 items] -> [4 items]")


def test_distribution_for_a_new_behavior():
    rows = dict(summary.distribution({"cases": [{"ok": True}] * 70 + [{"ok": False}] * 10}))
    assert summary.distribution_line(rows["cases[*].ok"]) == \
        "80 values, 2 distinct: true x70, false x10"


def test_review_summary_prints_paths_and_counts_as_reviewed(tmp_path):
    old, new = eval_payloads()
    store = store_with(tmp_path, old, new)
    recompute(store)
    r = cli("review", "--summary", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "cases[*].correct" in r.stdout and "78/80 changed" in r.stdout
    assert "--- approved" not in r.stdout          # no full diff
    # The summary shows every changed path, like a (capped) diff: it marks the
    # change reviewed, so approve accepts exactly this token (D19).
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout


def test_review_summary_of_new_and_removed(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name="gone", payload={"v": 1}))
    store.approve("gone")
    store.clear_pending()
    store.write_pending(Behavior(name="fresh", payload={"rows": [1, 1, 2]}))
    recompute(store)
    r = cli("review", "--summary", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "rows[*]" in r.stdout and "3 values, 2 distinct" in r.stdout
    assert "removed; it was:" in r.stdout


def test_truncated_diff_points_at_summary(tmp_path):
    old = {"xs": list(range(200))}
    new = {"xs": [x + 1 for x in range(200)]}
    store = store_with(tmp_path, old, new)
    recompute(store)
    r = cli("review", "--max-lines", "5", cwd=tmp_path)
    assert "--summary" in r.stdout.replace("\n", "")


# ---- capture environment ------------------------------------------------------


def test_env_is_recorded_but_never_fingerprinted():
    a = Behavior(name="n", payload={"v": 1}, env=WINDOWS)
    b = Behavior(name="n", payload={"v": 1}, env=LINUX)
    assert a.fingerprint() == b.fingerprint() and a == b
    assert Behavior.from_dict(a.to_dict()).env == WINDOWS
    assert "env" not in Behavior(name="n", payload=1).to_dict()   # old files stay stable
    assert Behavior.from_dict({"name": "n", "payload": 1, "env": "junk"}).env is None


def test_recorder_stamps_the_capture_env(tmp_path):
    rec = Recorder()
    rec.add("n", {"v": 1})
    assert rec.behaviors[0].env == capture_env()


def test_env_mismatch_names_changed_behaviors_approved_elsewhere(tmp_path):
    store = store_with(tmp_path, {"p": "a\\b"}, {"p": "a/b"},
                       old_env=WINDOWS, new_env=LINUX)
    report = recompute(store)
    assert report["boundary"] == "breached"            # a hint, never a verdict
    assert report["env_mismatch"] == {"capture": LINUX, "behaviors": {"evals": WINDOWS}}
    assert status_payload(report)["env_mismatch"] == report["env_mismatch"]
    r = cli("gate", cwd=tmp_path)
    assert r.returncode == 1
    assert "Windows / Python 3.12" in r.stderr and "Linux / Python 3.12" in r.stderr


def test_env_mismatch_is_silent_without_a_change_or_a_difference(tmp_path):
    same = store_with(tmp_path / "a", {"v": 1}, {"v": 1}, old_env=WINDOWS, new_env=LINUX)
    assert recompute(same)["env_mismatch"] is None          # intact: nothing to explain
    here = store_with(tmp_path / "b", {"v": 1}, {"v": 2}, old_env=LINUX, new_env=LINUX)
    assert recompute(here)["env_mismatch"] is None
    legacy = store_with(tmp_path / "c", {"v": 1}, {"v": 2}, old_env=None, new_env=LINUX)
    assert recompute(legacy)["env_mismatch"] is None        # approved before env existed


# ---- gate --run ---------------------------------------------------------------


def test_gate_run_judges_the_code_as_it_is_now(tmp_path):
    test = tmp_path / "test_app.py"
    test.write_text('def test_a(behavior):\n    behavior("a", {"v": 1})\n', encoding="utf-8")
    assert cli("init", cwd=tmp_path).returncode == 0
    cli("run", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    assert cli("gate", "--run", cwd=tmp_path).returncode == 0

    test.write_text('def test_a(behavior):\n    behavior("a", {"v": 2})\n', encoding="utf-8")
    assert cli("gate", cwd=tmp_path).returncode == 0       # the old verdict
    r = cli("gate", "--run", cwd=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "boundary breached" in r.stdout
    pending = json.loads((tmp_path / ".nightward" / "pending" / "a.received.json")
                         .read_text(encoding="utf-8"))
    assert pending["payload"] == {"v": 2} and pending["env"] == capture_env()


def test_gate_run_needs_a_store(tmp_path):
    r = cli("gate", "--run", cwd=tmp_path)
    assert r.returncode == 2 and "no nightward store" in r.stderr
