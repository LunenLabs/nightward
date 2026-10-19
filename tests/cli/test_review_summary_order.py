"""review --summary keeps list order visible.

From the PetTracker dogfood: a walker search returns a ranking, and the summary
folded it into `default_3km[*].name` with the names counted - which walker is
listed first, the thing to review, was gone. A pure reorder read as "3/4
changed" field values instead of "the order changed".
"""
import os
import subprocess
import sys

from nightward.core import summary
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute


def walker(name, km, rating):
    return {"avg_rating": rating, "distance_km": km, "name": name}


A, B, C = walker("near", 0.5, 5.0), walker("far", 8.0, 5.0), walker("mid", 1.0, 4.5)


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def test_a_new_ranking_shows_its_order():
    rows, hidden = summary.order_rows(None, {"radius_10km": [A, B, C], "other_day": []})
    assert rows == [("radius_10km", "order: near, far, mid")]
    assert hidden == set()


def test_a_pure_reorder_is_one_row_and_hides_the_field_rows():
    rows, hidden = summary.order_rows({"top": [A, B, C]}, {"top": [A, C, B]})
    assert rows == [("top", "reordered: near, far, mid -> near, mid, far")]
    assert hidden == {"top"}
    field_rows = [s.path for s in summary.changes({"top": [A, B, C]}, {"top": [A, C, B]})]
    assert field_rows and all(summary.is_hidden(p, hidden) for p in field_rows)


def test_a_changed_value_and_a_new_order_show_both():
    moved = walker("mid", 1.0, 4.9)
    rows, hidden = summary.order_rows({"top": [A, B, C]}, {"top": [A, moved, B]})
    assert rows == [("top", "order: near, far, mid -> near, mid, far")]
    assert hidden == set()          # the rating change still gets its field row


def test_same_order_has_no_row():
    moved = walker("far", 8.0, 4.0)
    assert summary.order_rows({"top": [A, B]}, {"top": [A, moved]}) == ([], set())


def test_long_lists_are_left_as_today():
    old = [{"name": f"w{i}"} for i in range(21)]
    assert summary.order_rows(None, {"xs": old}) == ([], set())
    assert summary.order_rows({"xs": old}, {"xs": old[::-1]}) == ([], set())


def test_labels_scalars_by_value_and_unlabelled_dicts_by_old_position():
    assert summary.order_rows(None, {"xs": [3, "b", None]})[0] == [
        ("xs", 'order: 3, b, null')]
    rows, hidden = summary.order_rows({"xs": [{"v": 1}, {"v": 2}, {"v": 3}]},
                                      {"xs": [{"v": 3}, {"v": 1}, {"v": 2}]})
    assert rows == [("xs", "reordered: #0, #1, #2 -> #2, #0, #1")]
    assert hidden == {"xs"}
    assert summary.order_rows(None, {"xs": [{"v": 1}, {"v": 2}]}) == ([], set())


def test_review_summary_prints_the_order(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name="search", payload={"top": [A, B, C]}, group="s"))
    store.approve("search")
    store.write_pending(Behavior(name="search", payload={"top": [A, C, B]}, group="s"))
    recompute(store)
    r = cli("review", "--summary", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "reordered: near, far, mid -> near, mid, far" in r.stdout
    assert "top[*]" not in r.stdout
