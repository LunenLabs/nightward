"""Beta round 4, D28 (R4-WEB-01, R4-LLM-02): custom scrub rules are test-owned
and visible. A rule registered in a test module is scoped to that module's
directory like a conftest rule; a rule registered from non-test code is refused;
`run` reports how many values each custom rule replaced when that changes."""
import subprocess
import sys
from pathlib import Path

import pytest

from nightward import scrub
from nightward.core.baseline import Store
from nightward.errors import NightwardError
from nightward.runner import execute_run

ORDERS_TEST = """
import secrets
from nightward import scrub
scrub.register_field("token")   # orders' session tokens are random

def test_order_session(behavior):
    behavior("orders.session", {"user": "u1", "token": secrets.token_hex(8)}, group="orders")
"""
BILLING_TEST = """
import os
def test_card_token(behavior):
    behavior("billing.card", {"last4": "4242", "token": os.environ.get("TOK", "tok_visa")},
             group="billing")
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def cli(*args, cwd, **env):
    import os
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env={**os.environ, **env})


# --- R4-WEB-01: a test-module rule is scoped to its directory --------------------


def test_a_test_module_rule_does_not_mask_another_services_field(tmp_path, monkeypatch):
    _write(tmp_path / "services" / "orders" / "test_orders.py", ORDERS_TEST)
    _write(tmp_path / "services" / "billing" / "test_billing.py", BILLING_TEST)
    dir_ = str(tmp_path / ".nightward")
    store = Store(Path(dir_))
    execute_run(str(tmp_path), dir_)
    pending = store.load_pending()
    assert pending["orders.session"].payload["token"] == "<SCRUBBED>"
    assert pending["billing.card"].payload["token"] == "tok_visa"
    whole = pending["billing.card"].fingerprint()
    execute_run(str(tmp_path / "services" / "billing"), dir_)
    assert store.load_pending()["billing.card"].fingerprint() == whole


def test_a_rule_registered_inside_a_test_function_is_scoped_too(tmp_path):
    scrub.register_field("token")          # this test module: tests/
    here = Path(__file__).parent
    assert scrub.scrub_counted({"token": "x"}, path=here / "sub" / "test_a.py")[0] == {
        "token": "<SCRUBBED>"}
    assert scrub.scrub_counted({"token": "x"}, path=tmp_path / "test_b.py")[0] == {
        "token": "x"}


# --- D28: rules from non-test code are refused ------------------------------------


def test_a_rule_registered_from_product_code_is_refused(tmp_path):
    _write(tmp_path / "triage" / "__init__.py",
           "from nightward import scrub\n"
           "scrub.register(r'refunds-auto', 'refunds-manager')\n")
    _write(tmp_path / "test_t.py", "import triage\n"
                                   "def test_t(behavior):\n    behavior('t', 'refunds-auto')\n")
    with pytest.raises(NightwardError) as exc:
        execute_run(str(tmp_path), str(tmp_path / ".nightward"), capture_output=True)
    assert "triage" in str(exc.value) and "conftest.py" in str(exc.value)


@pytest.mark.parametrize("call", ["register('x', '<X>')", "register_field('x')",
                                  "disable_defaults()"])
def test_registration_from_a_non_test_file_raises(tmp_path, call):
    helper = tmp_path / "helpers.py"
    helper.write_text(f"from nightward import scrub\ndef go():\n    scrub.{call}\n",
                      encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        import importlib
        mod = importlib.import_module("helpers")
        with pytest.raises(NightwardError, match="helpers.py"):
            mod.go()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("helpers", None)


# --- R4-LLM-02: run reports per-rule replacement counts when they change ----------


def test_run_reports_a_rules_replacements_when_they_change(tmp_path):
    _write(tmp_path / "conftest.py",
           "from nightward import scrub\n"
           "scrub.register(r'refunds-auto', 'refunds-manager')\n")
    _write(tmp_path / "test_q.py",
           "import os\n"
           "def test_q(behavior):\n"
           "    behavior('route', {'queue': os.environ.get('Q', 'refunds-manager')})\n")
    cli("init", cwd=tmp_path)
    first = cli("run", ".", cwd=tmp_path)
    assert "matched nothing" in first.stderr               # 0 replacements (unchanged warning)
    cli("approve", "--all", cwd=tmp_path)
    regressed = cli("run", ".", cwd=tmp_path, Q="refunds-auto")
    assert "intact" in regressed.stdout                    # the rewrite hides it ...
    assert "register(r'refunds-auto')" in regressed.stderr  # ... but never silently
    assert "replaced 1 value(s)" in regressed.stderr and "route" in regressed.stderr
    assert "not a <PLACEHOLDER>" in regressed.stderr
    again = cli("run", ".", cwd=tmp_path, Q="refunds-auto")
    assert "replaced 1 value(s)" not in again.stderr        # unchanged count: quiet


def test_mcp_warnings_carry_the_custom_rule_counts(tmp_path):
    _write(tmp_path / "conftest.py", "from nightward import scrub\n"
                                     "scrub.register_field('token')\n")
    _write(tmp_path / "test_s.py", "def test_s(behavior):\n"
                                   "    behavior('s', {'token': 'abc'})\n")
    from nightward import mcp_server
    out = mcp_server.run_tool(str(tmp_path), str(tmp_path / ".nightward"))
    [rule] = out["warnings"]["scrub_rules"]
    assert rule["rule"].startswith("register_field('token')")
    assert rule["values"] == 1 and rule["behaviors"] == ["s"] and rule["changed"] is True
