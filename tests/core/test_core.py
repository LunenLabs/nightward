"""Unit tests for the diff + blast-radius core (no pytest plugin needed)."""
from nightward.core.behavior import Behavior
from nightward.core.blast import aggregate
from nightward.core.diff import CHANGED, NEW, REMOVED, UNCHANGED, compare


def b(name, payload, group=None):
    return Behavior(name, payload, group)


def test_unchanged():
    base = {"x": b("x", {"v": 1})}
    pend = {"x": b("x", {"v": 1})}
    assert compare(base, pend)[0].kind == UNCHANGED


def test_changed_new_removed():
    base = {"x": b("x", {"v": 1}), "y": b("y", {"v": 2})}
    pend = {"x": b("x", {"v": 99}), "z": b("z", {"v": 3})}
    kinds = {c.name: c.kind for c in compare(base, pend)}
    assert kinds["x"] == CHANGED
    assert kinds["y"] == REMOVED
    assert kinds["z"] == NEW


def test_blast_breached():
    report = aggregate(compare({"x": b("x", {"v": 1})}, {"x": b("x", {"v": 2})}))
    assert report["boundary"] == "breached"
    assert report["unapproved"] == 1
    assert report["counts"]["changed"] == 1


def test_blast_intact():
    report = aggregate(compare({"x": b("x", {"v": 1})}, {"x": b("x", {"v": 1})}))
    assert report["boundary"] == "intact"
    assert report["unapproved"] == 0


def test_grouping():
    base = {}
    pend = {"a": b("a", {"v": 1}, group="billing"), "b": b("b", {"v": 2}, group="auth")}
    report = aggregate(compare(base, pend))
    assert set(report["blast_radius"]) == {"billing", "auth"}
