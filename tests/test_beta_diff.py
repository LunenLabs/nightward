"""Beta round 1: diff cost and readability (R1-FIN-02, R1-WEB-04, R1-OPS-05).

A failure here is a real defect - fix the code, do not weaken the test.
"""
import difflib
import json
import time

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, canonical_json
from nightward.core.diff import MAX_DIFF_LINES, _line_diff, compare


def rows(n, bump=0, every=3):
    return [{"txn": f"txn_{i:07d}", "amount_cents": (i * 37) % 100000 + bump * (i % every == 0)}
            for i in range(n)]


def diff_of(old, new):
    (change,) = compare({"x": Behavior("x", old)}, {"x": Behavior("x", new)})
    return change


# ---- R1-FIN-02: diff cost stays near-linear ---------------------------------

def test_scattered_changes_in_a_big_list_diff_fast():
    # 8,000 rows with every 3rd row changed took ~18 s (quadratic difflib).
    start = time.perf_counter()
    change = diff_of(rows(8000), rows(8000, bump=1))
    assert time.perf_counter() - start < 3
    assert change.kind == "CHANGED"
    lines = change.diff_text.splitlines()
    assert len(lines) <= MAX_DIFF_LINES + 5
    assert "truncated" in lines[-1]


def test_new_huge_behavior_is_capped():
    (change,) = compare({}, {"x": Behavior("x", rows(50000))})
    lines = change.diff_text.splitlines()
    assert len(lines) <= MAX_DIFF_LINES + 5 and "truncated" in lines[-1]


def test_single_change_in_huge_list_is_pinpointed():
    old, new = rows(50000), rows(50000)
    new[31337] = {"txn": "txn_0031337", "amount_cents": -1}
    start = time.perf_counter()
    text = diff_of(old, new).diff_text
    assert time.perf_counter() - start < 3
    assert '-    "amount_cents": ' in text and '+    "amount_cents": -1' in text
    assert len(text.splitlines()) < 15


@pytest.mark.parametrize("a, b", [
    (list("abcdefghij"), list("abcXefghij")),          # middle replace
    (list("abcdefghij"), list("Xbcdefghij")),          # first line
    (list("abcdefghij"), list("abcdefghiX")),          # last line
    (list("abcdefghij"), list("abcdeYYfghij")),        # insertion
    (list("abcdefghij"), list("abcghij")),             # deletion
    (list("abcdefghijklmnopqrstu"), list("aXcdefghijklmnopqrsYu")),  # two hunks
    ([], list("abc")),
    (list("abc"), []),
])
def test_line_diff_matches_difflib_on_small_inputs(a, b):
    expected = list(difflib.unified_diff(a, b, fromfile="approved", tofile="received",
                                         lineterm=""))
    assert _line_diff(a, b) == expected


def test_equal_length_fallback_hunks_are_well_formed():
    old, new = rows(3000), rows(3000, bump=1, every=500)
    text = diff_of(old, new).diff_text
    hunks = [ln for ln in text.splitlines() if ln.startswith("@@")]
    assert len(hunks) == 6
    assert "-    \"amount_cents\": 0" in text and "+    \"amount_cents\": 1" in text


# ---- R1-WEB-04 / R1-OPS-05: multi-line strings diff line by line ------------

def invoice(price):
    body = "\n".join(f"  <tr><td>item {i}</td><td>{price if i == 42 else '5.00'}</td></tr>"
                     for i in range(80))
    return {"status": 200, "body": f"<html>\n<body>\n<table>\n{body}\n</table>\n</body>\n</html>"}


def test_multiline_string_in_dict_diffs_per_line():
    text = diff_of(invoice("19.00"), invoice("91.00")).diff_text
    changed = [ln for ln in text.splitlines()
               if ln[:1] in "+-" and not ln.startswith(("---", "+++"))]
    assert changed == ["-      <tr><td>item 42</td><td>19.00</td></tr>",
                       "+      <tr><td>item 42</td><td>91.00</td></tr>"]
    assert "item 0<" not in text


def manifest(port):
    return "\n".join(["apiVersion: apps/v1", "kind: Deployment", "spec:", "  ports:",
                      f"    - containerPort: {port}", "  env: []"]) + "\n"


def test_top_level_multiline_string_diffs_per_line():
    text = diff_of(manifest(8000), manifest(8001)).diff_text
    assert "-      - containerPort: 8000" in text
    assert "+      - containerPort: 8001" in text
    assert len(text.splitlines()) < 15


def test_trailing_newline_only_change_still_shows():
    text = diff_of("a\nb", "a\nb\n").diff_text
    assert any(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
               for ln in text.splitlines())


def test_string_that_mimics_the_expansion_still_diffs():
    text = diff_of({"s": "x\ny"}, {"s": 'x\ny', "t": '"""'}).diff_text
    assert '+  "t": "\\"\\"\\""' in text


# ---- baseline files: human-diffable, fingerprints unchanged ------------------

def test_store_files_end_with_newline_and_keep_fingerprints(tmp_path):
    b = Behavior("m", manifest(8000), group="g")
    store = Store(tmp_path)
    store.replace_pending([b])
    store.approve("m")
    text = (tmp_path / "baseline" / "m.approved.json").read_text(encoding="utf-8")
    assert text.endswith("}\n")
    # an existing baseline written without the newline loads to the same fingerprint
    (tmp_path / "baseline" / "m.approved.json").write_text(
        canonical_json(b.to_dict()), encoding="utf-8")
    assert store.load_baseline()["m"].fingerprint() == b.fingerprint()
    assert json.loads(text)["payload"] == b.payload


def test_approve_all_does_not_render_diffs():
    # approve --all only needs the verdicts
    (change,) = compare({"x": Behavior("x", 1)}, {"x": Behavior("x", 2)}, with_diff=False)
    assert change.kind == "CHANGED" and change.diff_text == ""
