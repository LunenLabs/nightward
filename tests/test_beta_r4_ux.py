"""Beta round 4: dashboard/docs defects reported by beta testers, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

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


# ---- R4-WEB-04: the group chip says what `approve --group` covers --------------

D = {"name": "grp.d", "kind": "CHANGED"}
N = {"name": "grp.new", "kind": "NEW"}
R = {"name": "grp.old", "kind": "REMOVED"}


def chip(all_items, visible):
    return node_eval(f"groupHead({json.dumps(all_items)}, {json.dumps(visible)})")


def test_group_chip_names_the_changes_the_filter_hides():
    head = chip([D, N], [N])
    assert "approve all 2 NEW/CHANGED" in head["label"]
    assert "1 hidden by your filters" in head["label"]
    assert head["count"] == "1 of 2 item(s) shown"


def test_group_chip_is_plain_when_everything_is_shown():
    head = chip([D, N], [D, N])
    assert head["label"] == "approve this group"
    assert head["count"] == "2 item(s)"


def test_group_chip_still_points_removals_at_their_cards():
    head = chip([D, R], [D, R])
    assert "approve all 1 NEW/CHANGED" in head["label"]
    assert "removals: approve each on its card" in head["label"]
    assert "hidden" not in head["label"]


def test_group_chip_counts_a_hidden_removal_only_as_hidden_not_as_approved():
    head = chip([D, N, R], [N])
    assert "approve all 2 NEW/CHANGED" in head["label"]
    assert "1 hidden by your filters" in head["label"]   # grp.d; the removal isn't approved


def test_group_without_new_or_changed_has_no_chip():
    assert chip([R], [R])["label"] is None


# ---- R4-FIN-05: onboarding says to commit the decisions, not just the baseline --

README = Path(__file__).parents[1] / "README.md"


def cli(*args, cwd):
    import sys
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def test_quickstart_commits_the_judge_ledger_and_rejections():
    text = README.read_text(encoding="utf-8")
    quickstart = text.split("```bash", 1)[1].split("```", 1)[0]
    adds = [ln for ln in quickstart.splitlines() if ln.startswith("git add")]
    assert adds, quickstart
    line = adds[0]
    assert "judge/" in line and "rejected/" in line, line
    paths = line.split("#", 1)[0].split()[2:]
    # either the whole store (init ignores the per-run files) or every committed dir
    assert ".nightward" in paths or {".nightward/judge", ".nightward/rejected"} <= set(paths)


@pytest.mark.parametrize("with_baseline", [False, True])
def test_init_next_step_names_everything_to_commit(tmp_path, with_baseline):
    if with_baseline:
        from nightward.core.baseline import Store
        from nightward.core.behavior import Behavior
        store = Store(tmp_path / ".nightward")
        store.ensure()
        store.write_pending(Behavior(name="a", payload=1))
        store.approve("a")
    r = cli("init", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = " ".join(r.stdout.split())
    for part in ("baseline/", "rejected/", "judge/"):
        assert part in out, out


# ---- round 3 follow-up: replayed rulings and boundary values this page predates --

REPLAYED = "replayed from the committed ledger, not ruled this run"


def test_replayed_ruling_is_marked():
    assert node_eval("replayedNote({judged: true, judge_replayed: true})") == REPLAYED
    assert node_eval("replayedNote({judged: true})") is None


def banner(report, meta=None):
    return node_eval(f"bannerText({json.dumps(report)}, "
                     f"bannerState({json.dumps(report)}, {json.dumps(meta or {})}))")


def test_partial_boundary_is_not_done():
    b = banner({"boundary": "partial", "unapproved": 0,
                "not_run": [{"name": "a", "group": None}, {"name": "b", "group": None}]})
    assert b["cls"] == "untrusted partial"
    assert "not checked" in b["title"].lower() or "partial" in b["title"].lower()
    assert "2" in b["explain"] and "--allow-not-run" in b["explain"]
    assert "passes" not in b["explain"]


def test_unknown_boundary_value_is_shown_raw_and_not_done():
    b = banner({"boundary": "<b>wobbly</b>", "unapproved": 0})
    assert b["cls"] == "untrusted other"
    assert "<b>wobbly</b>" in b["title"]
    assert "not done" in b["explain"]


def test_only_intact_is_a_safe_place_to_stop():
    for state in ("partial", "other", "incomplete"):
        assert "NOT a safe place to stop" in node_eval(f"noChangeText({json.dumps(state)})")
    assert "safe place to stop" in node_eval("noChangeText('intact')")
    assert "NOT" not in node_eval("noChangeText('intact')")


def test_known_states_keep_their_banner():
    assert banner({"boundary": "intact", "unapproved": 0})["cls"] == "intact"
    b = banner({"boundary": "breached", "unapproved": 3})
    assert b["cls"] == "breached" and b["count"] == "3 unapproved"
