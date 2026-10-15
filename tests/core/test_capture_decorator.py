"""@capture: a test's return value is the behavior it captures.

Sugar over the `behavior` fixture, with the same rules: names are validated and
unique, the payload is scrubbed and must be JSON, and approval stays human.
"""
import textwrap

from nightward.core.baseline import Store
from nightward.runner import execute_run


def _run(tmp_path, source: str, *pytest_args: str) -> dict:
    (tmp_path / "test_app.py").write_text(textwrap.dedent(source), encoding="utf-8")
    result = execute_run(str(tmp_path), str(tmp_path / ".nightward"), capture_output=True,
                         pytest_args=list(pytest_args))
    result["pending"] = Store(tmp_path / ".nightward").load_pending()
    return result


def test_the_return_value_is_captured_under_the_test_name(tmp_path):
    r = _run(tmp_path, """
        from nightward import capture

        @capture(group="billing")
        def test_checkout():
            return {"total": 28.05}
    """)
    assert r["pytest_returncode"] == 0, r["output_tail"]
    b = r["pending"]["test_checkout"]
    assert (b.payload, b.group) == ({"total": 28.05}, "billing")
    assert b.source == "test_app.py::test_checkout"


def test_bare_decorator_and_an_explicit_name(tmp_path):
    r = _run(tmp_path, """
        from nightward import capture

        @capture
        def test_login():
            return {"status": "ok"}

        @capture(name="checkout_total")
        def test_checkout():
            return 28.05
    """)
    assert r["pytest_returncode"] == 0, r["output_tail"]
    assert r["pending"]["test_login"].payload == {"status": "ok"}
    assert r["pending"]["test_login"].group is None
    assert r["pending"]["checkout_total"].payload == 28.05


def test_each_parametrized_case_is_its_own_behavior(tmp_path):
    r = _run(tmp_path, """
        import pytest
        from nightward import capture

        @capture(name="tax", group="billing")
        @pytest.mark.parametrize("rate", [0.1, 0.2], ids=["low", "high"])
        def test_tax(rate):
            return round(100 * rate, 2)
    """)
    assert r["pytest_returncode"] == 0, r["output_tail"]
    assert {n: b.payload for n, b in r["pending"].items()} == {"tax[low]": 10.0,
                                                                 "tax[high]": 20.0}


def test_no_return_not_none_warning_even_as_an_error(tmp_path):
    r = _run(tmp_path, """
        from nightward import capture

        @capture
        def test_value():
            return [1, 2]
    """, "-W", "error::pytest.PytestReturnNotNoneWarning", "--strict-markers")
    assert r["pytest_returncode"] == 0, r["output_tail"]
    assert r["pending"]["test_value"].payload == [1, 2]


def test_returning_none_fails_the_test_instead_of_capturing_null(tmp_path):
    r = _run(tmp_path, """
        from nightward import capture

        @capture
        def test_forgot_return():
            {"total": 1}
    """)
    assert r["pytest_returncode"] == 1
    assert "returned None" in r["output_tail"]
    assert r["report"]["incomplete"]          # a failing capture test is never green


def test_works_with_fixtures_and_the_behavior_fixture(tmp_path):
    r = _run(tmp_path, """
        import pytest
        from nightward import capture

        @pytest.fixture
        def cart():
            return [10, 5]

        @capture(group="billing")
        def test_checkout(cart, behavior):
            behavior("checkout_lines", len(cart), group="billing")
            return sum(cart)
    """)
    assert r["pytest_returncode"] == 0, r["output_tail"]
    assert {n: b.payload for n, b in r["pending"].items()} == {"checkout_lines": 2,
                                                                 "test_checkout": 15}


def test_flags_reach_the_behavior(tmp_path):
    r = _run(tmp_path, """
        from nightward import capture

        @capture(scrub=False)
        def test_stamp():
            return {"at": "2026-10-15T12:00:00Z"}

        @capture(semantic=True)
        def test_summary():
            return "Two orders shipped."
    """)
    assert r["pytest_returncode"] == 0, r["output_tail"]
    assert r["pending"]["test_stamp"].payload == {"at": "2026-10-15T12:00:00Z"}
    assert r["pending"]["test_summary"].semantic is True


def test_an_async_test_is_refused_with_guidance(tmp_path):
    r = _run(tmp_path, """
        from nightward import capture

        @capture
        async def test_fetch():
            return 1
    """)
    assert r["pytest_returncode"] == 1
    assert "async" in r["output_tail"] and "behavior" in r["output_tail"]


def test_bad_arguments_fail_at_decoration_time():
    import pytest

    from nightward import capture
    from nightward.errors import NightwardError

    with pytest.raises(NightwardError, match="name"):
        capture(name="has space")
    with pytest.raises(TypeError):
        capture("checkout")   # a positional name would read as the test function


def test_a_unittest_method_is_refused_not_silently_skipped(tmp_path):
    import subprocess
    import sys

    import pytest

    from nightward.errors import NightwardError

    source = """
        import unittest
        from nightward import capture

        class TestCheckout(unittest.TestCase):
            @capture
            def test_total(self):
                return 28.05
    """
    with pytest.raises(NightwardError, match="could not load"):   # aborted, not a quiet pass
        _run(tmp_path, source)
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_app.py"], cwd=tmp_path,
                         capture_output=True, text=True)
    assert out.returncode == 4
    assert "not unittest.TestCase methods" in out.stdout + out.stderr
