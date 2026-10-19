"""Review depth: how much a human reads and when they are asked - never whether.

docs/superpowers/specs/2026-10-20-review-depth-and-onboarding-design.md. No
depth approves anything: the gate stays breached until a human approves.
"""
import json
import os
import subprocess
import sys

import pytest

from nightward.config import ReviewDepths, review_depths
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.errors import NightwardError
from nightward.runner import recompute

DEPTHS = """
[tool.nightward.review]
default = "standard"

[tool.nightward.review.groups]
pay = "exhaustive"
search = "detailed"
debug = "brief"
docs = "light"
"""


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def store_with(tmp_path, changes: dict, *, new: dict | None = None, depths: str = DEPTHS):
    """changes: name -> (group, old, new) approved then changed; new: name -> (group, value)."""
    (tmp_path / "pyproject.toml").write_text(depths, encoding="utf-8")
    store = Store(tmp_path / ".nightward")
    store.ensure()
    for name, (group, old, _) in changes.items():
        store.write_pending(Behavior(name=name, payload=old, group=group))
        store.approve(name)
    for name, (group, _, value) in changes.items():
        store.write_pending(Behavior(name=name, payload=value, group=group))
    for name, (group, value) in (new or {}).items():
        store.write_pending(Behavior(name=name, payload=value, group=group))
    recompute(store)
    return store


# ---- configuration --------------------------------------------------------------


def test_no_setting_is_standard_everywhere(tmp_path):
    d = review_depths(tmp_path / ".nightward")
    assert (d.default, d.of("any"), d.of(None)) == ("standard", "standard", "standard")


def test_groups_and_the_default(tmp_path):
    (tmp_path / "pyproject.toml").write_text(DEPTHS, encoding="utf-8")
    d = review_depths(tmp_path / ".nightward")
    assert [d.of(g) for g in ("pay", "search", "debug", "docs", "other")] == [
        "exhaustive", "detailed", "brief", "light", "standard"]


@pytest.mark.parametrize("bad", [
    '[tool.nightward.review]\ndefault = "quick"',
    '[tool.nightward.review.groups]\npay = "full"',
    '[tool.nightward]\nreview = "brief"',
    '[tool.nightward.review]\ngroups = ["pay"]',
])
def test_a_wrong_setting_names_the_file(tmp_path, bad):
    (tmp_path / "pyproject.toml").write_text(bad + "\n", encoding="utf-8")
    with pytest.raises(NightwardError, match=r"pyproject\.toml.*review"):
        review_depths(tmp_path / ".nightward")


def test_new_and_removed_never_go_below_standard_and_at_least_only_raises():
    d = ReviewDepths("brief", {"pay": "exhaustive"})
    assert d.of("x", "CHANGED") == "brief"
    assert d.of("x", "NEW") == d.of("x", "REMOVED") == "standard"
    assert d.of("x", "CHANGED", at_least="detailed") == "detailed"
    assert d.of("pay", "CHANGED", at_least="brief") == "exhaustive"   # never lowers


# ---- what agents and CI read ------------------------------------------------------


def test_status_carries_depth_and_when_to_ask_and_the_verdict_is_unchanged(tmp_path):
    store_with(tmp_path, {"p": ("pay", 1, 2), "g": ("debug", 1, 2), "s": ("other", 1, 2)},
               new={"fresh": ("debug", 1)})
    out = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    got = {c["name"]: (c["depth"], c["ask_human"]) for c in out["changes"]}
    assert got == {"p": ("exhaustive", "now"), "g": ("brief", "at_pr"),
                   "s": ("standard", "now"), "fresh": ("standard", "now")}
    assert out["boundary"] == "breached"
    assert cli("gate", cwd=tmp_path).returncode == 1        # brief still blocks


# ---- review ------------------------------------------------------------------------


def test_a_brief_group_is_one_line_but_its_new_behavior_gets_a_diff(tmp_path):
    store_with(tmp_path, {"a": ("debug", {"v": 1}, {"v": 2}), "b": ("debug", {"w": 1}, {"w": 3})},
               new={"fresh": ("debug", {"x": 1})})
    r = cli("review", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "[brief] 2 behavior(s) changed: a, b" in r.stdout
    assert '"v": 2' not in r.stdout                      # no diff for the brief ones
    assert "[NEW] fresh" in r.stdout and '"x": 1' in r.stdout
    assert "--depth standard" in r.stdout
    # What the depth displayed counts as shown: a human chose this depth.
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0


def test_depth_raises_a_brief_group_for_one_review(tmp_path):
    store_with(tmp_path, {"a": ("debug", {"v": 1}, {"v": 2})})
    r = cli("review", "--depth", "standard", cwd=tmp_path)
    assert '"v": 2' in r.stdout and "[brief]" not in r.stdout


def test_detailed_shows_the_whole_diff(tmp_path):
    store_with(tmp_path, {"long": ("search", list(range(200)), [x + 1 for x in range(200)])})
    r = cli("review", cwd=tmp_path)
    assert "more diff line(s)" not in r.stdout and "(detailed)" in r.stdout


def test_light_shows_the_summary(tmp_path):
    store_with(tmp_path, {"d": ("docs", {"cases": [{"ok": True}] * 5},
                                {"cases": [{"ok": False}] * 5})})
    r = cli("review", cwd=tmp_path)
    assert "cases[*].ok" in r.stdout and "5/5 changed" in r.stdout


def test_an_unknown_depth_is_an_error(tmp_path):
    store_with(tmp_path, {"a": ("debug", 1, 2)})
    r = cli("review", "--depth", "quick", cwd=tmp_path)
    assert r.returncode == 2 and "brief, light, standard" in r.stderr


# ---- approve ------------------------------------------------------------------------


def test_bulk_approval_leaves_exhaustive_behaviors_to_one_name_each(tmp_path):
    store_with(tmp_path, {"p": ("pay", 1, 2), "q": ("pay", 1, 3), "s": ("other", 1, 2)})
    assert cli("review", cwd=tmp_path).returncode == 0
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "kept (exhaustive)" in r.stdout and "p, q" in r.stdout
    assert "approved s" in r.stdout
    r = cli("approve", "--group", "pay", cwd=tmp_path)
    assert "kept (exhaustive)" in r.stdout
    assert cli("gate", cwd=tmp_path).returncode == 1
    assert cli("approve", "p", cwd=tmp_path).returncode == 0
    assert cli("approve", "q", cwd=tmp_path).returncode == 0
    assert cli("gate", cwd=tmp_path).returncode == 0


def test_a_depth_raised_after_the_run_applies_to_approve(tmp_path):
    store_with(tmp_path, {"s": ("other", 1, 2)}, depths="")
    assert cli("review", cwd=tmp_path).returncode == 0
    (tmp_path / "pyproject.toml").write_text(
        '[tool.nightward.review.groups]\nother = "exhaustive"\n', encoding="utf-8")
    r = cli("approve", "--all", cwd=tmp_path)
    assert "kept (exhaustive)" in r.stdout
