"""Capture coverage: what the gate can't see (spec 2026-10-15, option A).

A green gate over three captures of a 40-function app looked exactly like one
over three hundred. With `[tool.nightward] coverage` set, a run lists the public
functions no capture test ran. It is information only: never the verdict.
"""
import os
import subprocess
import sys

import pytest

from nightward.core.baseline import Store
from nightward.errors import NightwardError
from nightward.runner import execute_run
from nightward.signal import status_payload

APP = '''
import functools


def deco(f):
    @functools.wraps(f)
    def wrapper(*a):
        return f(*a)
    return wrapper


def total(cart):
    return sum(_price(i) for i in cart)


def _price(item):
    return item * 2


def refund(x):
    return -x


@deco
def discounted(x):
    return x - 1


class Cart:
    def add(self, x):
        return [x]

    def clear(self):
        return []

    def _secret(self):
        return 0


class _Internal:
    def visible(self):
        return 1
'''

TESTS = '''
from nightward import capture
from app.pricing import Cart, discounted, refund, total


def test_total(behavior):
    behavior("total", total([1, 2]), group="billing")


@capture(group="billing")
def test_discount():
    return discounted(5)


def test_add(behavior):
    behavior("cart", Cart().add(1), group="cart")


def test_refund_without_a_capture():
    assert refund(3) == -3     # runs refund, but captures nothing
'''

COVERED = ["app/pricing.py:Cart.add", "app/pricing.py:discounted", "app/pricing.py:total"]
# deco runs at import (collection), not while a capture test runs
UNCOVERED = ["app/pricing.py:Cart.clear", "app/pricing.py:deco", "app/pricing.py:refund"]


def project(tmp_path, setting='coverage = "app"'):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
        f"[tool.nightward]\n{setting}\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "pricing.py").write_text(APP, encoding="utf-8")
    (tmp_path / "app" / "test_inside.py").write_text(   # a test module in the source tree
        "def test_x():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cov.py").write_text(TESTS, encoding="utf-8")
    return tmp_path


def run(tmp_path):
    return execute_run("tests", str(tmp_path / ".nightward"), capture_output=True)


def test_lists_the_public_functions_no_capture_test_ran(tmp_path):
    result = run(project(tmp_path))
    cov = result["report"]["capture_coverage"]
    assert cov["roots"] == ["app"]
    assert cov["functions"] == len(COVERED) + len(UNCOVERED)
    assert cov["executed"] == len(COVERED)
    assert cov["uncovered"] == UNCOVERED


def test_a_test_that_captures_nothing_does_not_count(tmp_path):
    cov = run(project(tmp_path))["report"]["capture_coverage"]
    assert "app/pricing.py:refund" in cov["uncovered"]


def test_private_names_test_modules_and_nested_functions_are_left_out(tmp_path):
    cov = run(project(tmp_path))["report"]["capture_coverage"]
    listed = set(cov["uncovered"])
    assert not {n for n in listed if "_" in n.split(":")[1].split(".")[0][:1]}
    assert not [n for n in listed if "test_inside" in n or "wrapper" in n]


def test_coverage_never_changes_the_verdict(tmp_path):
    with_cov = run(project(tmp_path))["report"]
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["."]\n', encoding="utf-8")
    without = run(tmp_path)["report"]
    assert without["capture_coverage"] is None
    for key in ("boundary", "unapproved", "counts", "pending_digest", "baseline_digest"):
        assert with_cov[key] == without[key], key


def test_status_and_mcp_carry_it(tmp_path):
    run(project(tmp_path))
    store = Store(tmp_path / ".nightward")
    payload = status_payload(store.load_report())
    assert payload["capture_coverage"]["uncovered"] == UNCOVERED
    assert status_payload(None)["capture_coverage"] is None


def test_off_without_the_setting(tmp_path):
    (tmp_path / "test_a.py").write_text(
        'def test_a(behavior):\n    behavior("a", 1)\n', encoding="utf-8")
    report = execute_run("test_a.py", str(tmp_path / ".nightward"),
                         capture_output=True)["report"]
    assert report["capture_coverage"] is None


def test_a_list_of_roots(tmp_path):
    project(tmp_path, 'coverage = ["app"]')
    assert run(tmp_path)["report"]["capture_coverage"]["roots"] == ["app"]


@pytest.mark.parametrize("bad", ['"missing"', "3", "[]", '[""]'])
def test_a_bad_setting_is_refused(tmp_path, bad):
    project(tmp_path, f"coverage = {bad}")
    with pytest.raises(NightwardError, match=r"\[tool.nightward\] coverage"):
        run(tmp_path)


def test_run_prints_the_gap(tmp_path):
    project(tmp_path)
    r = subprocess.run([sys.executable, "-m", "nightward", "run", "tests"], cwd=str(tmp_path),
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ})
    out = r.stdout + r.stderr
    assert "capture coverage: 3 of 6 public functions in app ran in a capture test" in out
    assert "Cart.clear" in out and "refund" in out


def test_an_async_capture_test_counts(tmp_path):
    pytest.importorskip("pytest_asyncio")
    project(tmp_path)
    (tmp_path / "tests" / "test_cov.py").write_text('''
import pytest
from nightward import capture
from app.pricing import refund


@pytest.mark.asyncio
@capture(group="billing")
async def test_refund():
    return refund(3)
''', encoding="utf-8")
    cov = run(tmp_path)["report"]["capture_coverage"]
    assert "app/pricing.py:refund" not in cov["uncovered"]
    assert cov["executed"] == 1
