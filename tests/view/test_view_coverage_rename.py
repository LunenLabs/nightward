"""The dashboard shows what `run`/`review` already say: rename hints and capture coverage.

Both are information, never a verdict: a rename hint keeps the REMOVED card's
explicit `approve --remove` chip (D29), and coverage says "ran in a capture
test", never "protected".
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute
from nightward.view import build_site

ASSETS = Path(__file__).parents[2] / "src" / "nightward" / "view" / "assets"
APP_JS = ASSETS / "app.js"

# Just enough DOM for renderCard: elements with children, text and attributes.
FAKE_DOM = """
function node(tag) {
  return { tag: tag, children: [], attrs: {}, className: "", textContent: "",
    classList: { add() {}, remove() {} },
    appendChild(c) { this.children.push(c); return c; },
    setAttribute(k, v) { this.attrs[k] = v; },
    addEventListener() {} };
}
function text(n) {
  return n.textContent + n.children.map(text).join(" ");
}
const document = { createElement: node,
  createTextNode(t) { const n = node("#text"); n.textContent = t; return n; } };
"""


def node_eval(js_expr):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    runner = (
        "const vm = require('vm'), fs = require('fs');"
        "const ctx = {fetch: () => new Promise(() => {}), console};"
        "vm.createContext(ctx); vm.runInContext(process.argv[3], ctx);"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
        "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2], ctx)));"
    )
    r = subprocess.run([node, "-e", runner, str(APP_JS), js_expr, FAKE_DOM],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def js(value):
    return json.dumps(value, ensure_ascii=False)


# ---- rename hint -----------------------------------------------------------------

def test_new_side_names_the_removed_one():
    it = {"kind": "NEW", "name": "cart_total", "same_as": ["checkout_total"]}
    assert node_eval(f"sameAsNote({js(it)})") == \
        "same payload as REMOVED checkout_total: renamed?"


def test_removed_side_names_the_new_one():
    it = {"kind": "REMOVED", "name": "checkout_total", "same_as": ["cart_total"]}
    assert node_eval(f"sameAsNote({js(it)})") == \
        "its payload is captured as cart_total now: renamed?"


def test_no_hint_without_same_as():
    assert node_eval(f"sameAsNote({js({'kind': 'NEW', 'name': 'x'})})") is None
    assert node_eval(f"sameAsNote({js({'kind': 'NEW', 'name': 'x', 'same_as': []})})") is None


def test_many_names_are_counted_like_the_cli():
    it = {"kind": "REMOVED", "name": "a", "same_as": ["b", "c", "d", "e"]}
    assert node_eval(f"sameAsNote({js(it)})") == \
        "its payload is captured as b, c, d and 1 more now: renamed?"


def test_removed_card_shows_the_hint_and_keeps_the_explicit_remove_chip():
    it = {"kind": "REMOVED", "name": "checkout_total", "group": "billing",
          "same_as": ["cart_total"], "diff": ""}
    quoted = {"checkout_total": {"posix": "checkout_total", "powershell": "checkout_total",
                                 "cmd": "checkout_total"}}
    out = node_eval(f"setQuoting({js(quoted)}, 'posix'); text(renderCard({js(it)}))")
    assert "its payload is captured as cart_total now: renamed?" in out
    assert "nightward approve --remove checkout_total" in out   # still one card, by name (D29)


def test_a_card_without_a_hint_has_no_rename_line():
    it = {"kind": "CHANGED", "name": "x", "group": "g", "diff": ""}
    assert "renamed?" not in node_eval(f"text(renderCard({js(it)}))")


# ---- capture coverage ------------------------------------------------------------

COV = {"roots": ["src/shop"], "functions": 12, "executed": 9,
       "uncovered": ["shop.tax:round_half", "shop.cart:merge", "shop.cart:split"],
       "unreadable": []}


def test_coverage_headline_says_ran_not_protected():
    lines = node_eval(f"coverageLines({js(COV)})")
    assert lines["headline"] == \
        "9 of 12 public functions in src/shop ran in a capture test"
    assert "protect" not in json.dumps(lines)


def test_coverage_lists_what_no_capture_test_ran():
    lines = node_eval(f"coverageLines({js(COV)})")
    assert lines["missing"] == ("not run by any capture test (3): shop.tax:round_half, "
                                "shop.cart:merge, shop.cart:split - a change there can't "
                                "breach the gate")


def test_coverage_counts_beyond_the_shown_names():
    cov = {**COV, "functions": 300, "executed": 0,
           "uncovered": [f"m:f{i}" for i in range(200)]}   # the list is capped at 200
    missing = node_eval(f"coverageLines({js(cov)})")["missing"]
    assert missing.startswith("not run by any capture test (300): m:f0, ")
    assert "m:f7 and 292 more - " in missing


def test_full_coverage_has_no_missing_line():
    cov = {**COV, "executed": 12, "uncovered": []}
    assert node_eval(f"coverageLines({js(cov)})")["missing"] is None


def test_unreadable_files_are_named():
    cov = {**COV, "unreadable": ["src/shop/broken.py"]}
    assert node_eval(f"coverageLines({js(cov)})")["unreadable"] == \
        "could not parse: src/shop/broken.py"


def test_no_coverage_no_panel():
    assert node_eval("coverageLines(null)") is None


def test_the_page_has_a_coverage_section():
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="coverage"' in html


def test_data_json_carries_both(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name="checkout_total", payload={"t": 1}, group="billing"))
    store.approve("checkout_total")
    store.replace_pending([Behavior(name="cart_total", payload={"t": 1}, group="billing")])
    meta = store.load_run_meta()
    meta["coverage"] = COV
    store.write_run_meta(meta)
    recompute(store)
    out = build_site(store.root, tmp_path / "site")
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert data["report"]["capture_coverage"] == COV
    items = {it["name"]: it for g in data["report"]["blast_radius"].values() for it in g}
    assert items["cart_total"]["same_as"] == ["checkout_total"]
