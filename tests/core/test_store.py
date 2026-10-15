"""Store round-trips: capture -> approve -> change -> remove, plus error paths."""
import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, validate_name
from nightward.core.blast import aggregate
from nightward.core.diff import compare
from nightward.errors import NightwardError


def _seed(tmp_path, name="x", payload=None, group=None):
    store = Store(tmp_path)
    store.ensure()
    store.write_pending(Behavior(name, payload or {"v": 1}, group))
    return store


def test_approve_promotes_pending(tmp_path):
    store = _seed(tmp_path)
    store.approve("x")
    assert "x" in store.load_baseline()
    report = aggregate(compare(store.load_baseline(), store.load_pending()))
    assert report["boundary"] == "intact"


def test_change_is_detected_after_approval(tmp_path):
    store = _seed(tmp_path)
    store.approve("x")
    store.clear_pending()
    store.write_pending(Behavior("x", {"v": 999}))
    report = aggregate(compare(store.load_baseline(), store.load_pending()))
    assert report["boundary"] == "breached"
    assert report["counts"]["changed"] == 1


def test_approve_removal_drops_baseline(tmp_path):
    store = _seed(tmp_path)
    store.approve("x")
    store.approve_removal("x")
    assert "x" not in store.load_baseline()


def test_approve_missing_pending_raises(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    with pytest.raises(NightwardError):
        store.approve("ghost")


def test_corrupt_file_raises(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    (store.baseline_dir / "x.approved.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(NightwardError):
        store.load_baseline()


def test_load_report_roundtrip(tmp_path):
    store = Store(tmp_path)
    store.write_report({"boundary": "intact"})
    assert store.load_report() == {"boundary": "intact"}
    assert Store(tmp_path / "empty").load_report() is None


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a b", "x*y"])
def test_invalid_names_rejected(bad):
    with pytest.raises(NightwardError):
        validate_name(bad)


@pytest.mark.parametrize("ok", ["x", "checkout_total", "a-b.c", "User_Login2"])
def test_valid_names_accepted(ok):
    assert validate_name(ok) == ok
