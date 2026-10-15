"""Beta round 4: doctor on real parallel-engine drift (R3-DATA-04 reopened),
columnar frames (R4-DATA-03) and added/dropped rows (R4-DATA-04)."""
import math
import random

from nightward.core.behavior import Behavior
from nightward.core.doctor import diagnose


def _diag(old, new):
    return diagnose({"b": Behavior("b", old)}, {"b": Behavior("b", new)})["behaviors"]["b"]


def _digits(note: str) -> int:
    return int(note.split("{x:.")[1].split("g}")[0])


# --- R3-DATA-04 (reopened): sums of a parallel engine drift 7-15 ULPs --------------


def _mart(run: int):
    rows = [[cust, 400, cust * 1.37 + 0.1] for cust in range(5000)]
    if run:
        rng = random.Random(run)
        rng.shuffle(rows)
        for r in rows:
            if rng.random() < 0.75:
                for _ in range(rng.randint(1, 15)):
                    r[2] = math.nextafter(r[2], math.inf if rng.random() < 0.5 else -math.inf)
    return rows


def test_reordered_sums_with_up_to_15_ulp_drift_are_named_and_the_fix_is_verified():
    base = _mart(0)
    for run in (1, 2, 3, 4):
        [f] = _diag(base, _mart(run))
        assert f["kind"] == "order-only" and "float64 noise" in f["note"], f["note"]
        assert "$[*][2]" in f["note"] and "ULP" in f["note"]
        d = _digits(f["note"])

        def tamed(rows, d=d):
            return sorted([[float(f"{v:.{d}g}") if isinstance(v, float) else v for v in r]
                           for r in rows])
        assert tamed(base) == tamed(_mart(run))


def test_a_cent_change_among_reordered_sums_is_not_noise():
    old = [[c, 400, round(c * 13.37, 2)] for c in range(50)]
    new = [r[:] for r in old]
    random.Random(5).shuffle(new)
    new[0][2] = round(new[0][2] + 0.01, 2)
    assert all("noise" not in f["note"] for f in _diag(old, new))


def test_a_billion_with_a_cent_changed_is_never_float_noise():
    [f] = _diag({"total": 1_234_567_890.12}, {"total": 1_234_567_890.13})
    assert f["kind"] == "changed"


# --- R4-DATA-03: columns of one frame move together ---------------------------------


def _frame(shuffle=False, swap=False):
    cust = [f"c{i:02d}" for i in range(12)]
    rev = [round(100 + i * 7.5, 2) for i in range(12)]
    tier = ["gold" if i % 3 == 0 else "basic" for i in range(12)]
    if swap:
        rev[0], rev[1] = rev[1], rev[0]
    rows = list(zip(cust, rev, tier, strict=True))
    if shuffle:
        random.Random(3).shuffle(rows)
    return {"cust": [r[0] for r in rows], "revenue": [r[1] for r in rows],
            "tier": [r[2] for r in rows]}


def test_columns_reordered_together_get_one_row_finding_never_per_column_sorting():
    [f] = _diag(_frame(), _frame(shuffle=True))
    assert f["path"] == "$" and f["kind"] == "order-only"
    assert "3 columns" in f["note"] and "never sort the columns" in f["note"]
    assert "sort_values" in f["note"] or "records" in f["note"]


def test_one_column_permuted_alone_is_a_real_change_without_sort_advice():
    [f] = _diag(_frame(), _frame(swap=True))
    assert f["path"] == "revenue" and f["kind"] == "changed"
    assert "moved between rows" in f["note"] and "2 of 12" in f["note"]
    assert "sort" not in f["note"]


def test_following_the_advice_keeps_a_revenue_swap_visible():
    def advised(cols):   # sort rows jointly by the key column, as doctor says
        rows = sorted(zip(*cols.values(), strict=True))
        return [dict(zip(cols, r, strict=True)) for r in rows]
    assert advised(_frame(shuffle=True)) == advised(_frame())          # noise gone
    assert advised(_frame(shuffle=True, swap=True)) != advised(_frame())   # bug still seen


# --- R4-DATA-04: name the added or dropped rows -------------------------------------


def _rows():
    return [{"cust": f"c{i}", "rev": i * 10} for i in range(5)]


def test_an_appended_row_is_named():
    new = _rows() + [{"cust": "c99", "rev": 1}]
    [f] = _diag(_rows(), new)
    assert f["path"] == "$" and f["kind"] == "structural"
    assert "1 element added at [5]" in f["note"] and '"c99"' in f["note"]


def test_an_inserted_row_is_named_at_its_index():
    new = _rows()
    new.insert(2, {"cust": "c99", "rev": 1})
    [f] = _diag(_rows(), new)
    assert "1 element added at [2]" in f["note"] and "list length 5 -> 6" in f["note"]


def test_a_dropped_row_is_named():
    new = _rows()
    new.pop(2)
    [f] = _diag(_rows(), new)
    assert "1 element removed (was [2])" in f["note"] and '"c2"' in f["note"]


def test_an_added_row_in_a_shuffled_list_says_the_rest_moved():
    new = _rows()
    random.Random(1).shuffle(new)
    new.append({"cust": "c99", "rev": 1})
    [f] = _diag(_rows(), new)
    assert "1 element added" in f["note"] and "other 5 element(s) the same, in a new order" \
        in f["note"]


def test_nested_list_length_paths_keep_their_key():
    [f] = _diag({"items": [1, 2]}, {"items": [1, 2, 3]})
    assert f["path"] == "items" and "added at [2]: 3" in f["note"]
