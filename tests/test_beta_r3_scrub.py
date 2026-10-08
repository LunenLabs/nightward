"""Beta round 3, R3-WEB-06 (D20): a scrub rule registered in a conftest.py applies
only to behaviors captured by tests under that conftest's directory, so one
service's noise fix can't blind another service's gate, and a capture never
depends on which directories happened to be collected."""
import json
from pathlib import Path

from nightward import scrub
from nightward.core.baseline import Store
from nightward.runner import execute_run

ORDERS_CONFTEST = """
from nightward import scrub
scrub.register_field("token")      # orders' session tokens are random per request
"""
ORDERS_TEST = """
import secrets
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


def _monorepo(tmp_path: Path) -> tuple[str, Store]:
    _write(tmp_path / "services" / "orders" / "conftest.py", ORDERS_CONFTEST)
    _write(tmp_path / "services" / "orders" / "test_orders.py", ORDERS_TEST)
    _write(tmp_path / "services" / "billing" / "test_billing.py", BILLING_TEST)
    dir_ = str(tmp_path / ".nightward")
    return dir_, Store(Path(dir_))


def test_a_service_conftest_rule_does_not_mask_another_services_field(tmp_path, monkeypatch):
    dir_, store = _monorepo(tmp_path)
    execute_run(str(tmp_path), dir_)
    pending = store.load_pending()
    assert pending["orders.session"].payload["token"] == "<SCRUBBED>"
    assert pending["billing.card"].payload["token"] == "tok_visa"
    for name in pending:
        store.approve(name)
    monkeypatch.setenv("TOK", "tok_mastercard")       # billing's regression
    report = execute_run(str(tmp_path), dir_)["report"]
    assert report["boundary"] == "breached"
    assert [it["name"] for it in report["blast_radius"]["billing"]] == ["billing.card"]


def test_the_capture_does_not_depend_on_which_directories_were_collected(tmp_path):
    dir_, store = _monorepo(tmp_path)
    execute_run(str(tmp_path), dir_)
    whole = store.load_pending()["billing.card"].fingerprint()
    execute_run(str(tmp_path / "services" / "billing"), dir_)
    assert store.load_pending()["billing.card"].fingerprint() == whole


def test_a_root_conftest_rule_stays_global(tmp_path):
    dir_, store = _monorepo(tmp_path)
    _write(tmp_path / "conftest.py", "from nightward import scrub\n"
                                     "scrub.register_field('last4')\n")
    execute_run(str(tmp_path), dir_)
    assert store.load_pending()["billing.card"].payload["last4"] == "<SCRUBBED>"


def test_disable_defaults_in_a_service_conftest_is_scoped_too(tmp_path):
    _write(tmp_path / "a" / "conftest.py", "from nightward import scrub\n"
                                           "scrub.disable_defaults()\n")
    stamp = "2024-01-05T09:00:00+00:00"
    for svc in ("a", "b"):
        _write(tmp_path / svc / f"test_{svc}.py",
               f"def test_{svc}(behavior):\n    behavior('{svc}.at', {stamp!r})\n")
    dir_ = str(tmp_path / ".nightward")
    execute_run(str(tmp_path), dir_)
    pending = Store(Path(dir_)).load_pending()
    assert pending["a.at"].payload == stamp
    assert pending["b.at"].payload == "<TIMESTAMP>"


def test_an_unmatched_scoped_rule_names_its_conftest(tmp_path):
    _write(tmp_path / "svc" / "conftest.py", "from nightward import scrub\n"
                                             "scrub.register_field('nope')\n")
    _write(tmp_path / "svc" / "test_s.py", "def test_s(behavior):\n    behavior('s', {'a': 1})\n")
    result = execute_run(str(tmp_path), str(tmp_path / ".nightward"))
    [rule] = result["scrub_unmatched"]
    assert rule.startswith("register_field('nope')") and "conftest.py" in rule


def test_rules_registered_outside_a_conftest_stay_global():
    scrub.register_field("token")
    assert scrub.scrub_counted({"token": "x"}, path=Path("/elsewhere/test_x.py"))[0] == {
        "token": "<SCRUBBED>"}


def test_scoped_rule_applies_only_under_its_directory(tmp_path):
    conftest = tmp_path / "orders" / "conftest.py"
    scrub._register_field_scoped("token", "<SCRUBBED>", conftest)
    inside = tmp_path / "orders" / "sub" / "test_o.py"
    outside = tmp_path / "billing" / "test_b.py"
    assert scrub.scrub_counted({"token": "x"}, path=inside)[0] == {"token": "<SCRUBBED>"}
    assert scrub.scrub_counted({"token": "x"}, path=outside)[0] == {"token": "x"}
    assert scrub.scrub_counted({"token": "x"})[0] == {"token": "x"}   # unknown test


def test_doctor_names_the_conftest_to_put_a_rule_in(tmp_path):
    from nightward.core.behavior import Behavior
    from nightward.core.doctor import diagnose
    src = "services/orders/test_orders.py::test_order_session"
    base = {"s": Behavior("s", {"id": "req_8f3k2j9d0a"}, source=src)}
    pend = {"s": Behavior("s", {"id": "req_x7w2m4q8z1"}, source=src)}
    [s] = diagnose(base, pend)["suggestions"]
    assert s["conftest"] == "services/orders/conftest.py"
    json.dumps(s)   # stays JSON-friendly
