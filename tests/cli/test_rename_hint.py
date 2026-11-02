"""A rename reads as REMOVED + NEW with the same payload; say so, decide nothing.

Renaming `checkout_total` to `cart_total` used to show two unrelated changes, so a
human had to diff both to see they were one. Now each side names the other
(`same_as`). It is display only: a REMOVED behavior still leaves the baseline only
through `approve --remove` (D29), and the hint is in no token or verdict.
"""
import json
import os
import subprocess
import sys

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def renamed(tmp_path, old="checkout_total", new="cart_total", payload=None, new_payload=None):
    payload = {"total": 10} if payload is None else payload
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name=old, payload=payload, group="billing"))
    store.write_pending(Behavior(name="other", payload={"x": 1}, group="billing"))
    store.approve(old)
    store.approve("other")
    store.replace_pending([
        Behavior(name=new, payload=payload if new_payload is None else new_payload,
                 group="billing"),
        Behavior(name="other", payload={"x": 1}, group="billing")])
    return store, recompute(store)


def items(report):
    return {it["name"]: it for g in report["blast_radius"].values() for it in g}


def test_each_side_names_the_other(tmp_path):
    _, report = renamed(tmp_path)
    by = items(report)
    assert by["cart_total"]["same_as"] == ["checkout_total"]
    assert by["checkout_total"]["same_as"] == ["cart_total"]


def test_a_different_payload_is_no_rename(tmp_path):
    _, report = renamed(tmp_path, new_payload={"total": 11})
    assert not any("same_as" in it for it in items(report).values())


def test_the_hint_is_in_no_token(tmp_path):
    _, with_hint = renamed(tmp_path / "a")
    _, without = renamed(tmp_path / "b", new_payload={"total": 10, "pad": 0})
    old = items(with_hint)["checkout_total"]
    assert old["token"] == items(without)["checkout_total"]["token"]


def test_approve_all_still_keeps_the_removed_side(tmp_path):
    store, _ = renamed(tmp_path)
    assert cli("review", cwd=tmp_path).returncode == 0
    r = cli("approve", "--all", cwd=tmp_path)
    out = r.stdout + r.stderr
    assert "checkout_total" in store.load_baseline()           # D29: never in bulk
    assert "cart_total" in store.load_baseline()
    assert "same payload is now captured as cart_total" in out
    assert "nightward approve --remove checkout_total" in out


def test_run_review_and_status_show_it(tmp_path):
    renamed(tmp_path)
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    by = {c["name"]: c for c in status["changes"]}
    assert by["cart_total"]["same_as"] == ["checkout_total"]
    review = cli("review", cwd=tmp_path).stdout
    assert "same payload as REMOVED checkout_total" in review
    assert "its payload is captured as cart_total now: renamed?" in review


def test_run_lists_the_pair(tmp_path):
    (tmp_path / "test_a.py").write_text(
        'def test_a(behavior):\n    behavior("checkout_total", {"total": 10})\n',
        encoding="utf-8")
    cli("run", ".", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    cli("approve", "--all", cwd=tmp_path)
    (tmp_path / "test_a.py").write_text(
        'def test_a(behavior):\n    behavior("cart_total", {"total": 10})\n',
        encoding="utf-8")
    out = cli("run", ".", cwd=tmp_path).stdout
    assert "[NEW] cart_total (same payload as REMOVED checkout_total: renamed?)" in out


def test_the_removed_side_keeps_its_hint_after_the_new_side_is_approved(tmp_path):
    store, _ = renamed(tmp_path)
    store.approve("cart_total")
    by = items(recompute(store))
    assert set(by) == {"checkout_total"}
    assert by["checkout_total"]["same_as"] == ["cart_total"]
    review = cli("review", cwd=tmp_path).stdout
    assert "its payload is captured as cart_total now: renamed?" in review
