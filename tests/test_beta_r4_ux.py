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
