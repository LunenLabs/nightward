"""Beta round 1b: UX / message defects reported by beta testers, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import datetime
import decimal
import os
import subprocess
import sys

import pytest

from nightward import runner, scrub
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.errors import NightwardError
from nightward.pytest_plugin import Recorder
from nightward.runner import execute_run, recompute


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def breached_store(tw, n=800):
    """A store whose last report lists `n` CHANGED behaviors (no pytest needed)."""
    store = Store(tw)
    store.ensure()
    for i in range(n):
        b = Behavior(name=f"b{i:04d}", payload={"v": i, "w": [i, i + 1]}, group=f"g{i % 7}")
        store.write_pending(b)
        store.approve(b.name)
        store.write_pending(Behavior(name=b.name, payload={"v": i + 1, "w": [i, i + 2]},
                                     group=b.group))
    recompute(store)
    return store


def closed_reader(*args, cwd, read_bytes):
    """Run the CLI with stdout piped into a reader that quits early, like `| head`."""
    proc = subprocess.Popen([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if read_bytes:
        proc.stdout.read(read_bytes)
    proc.stdout.close()
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    proc.stderr.close()
    return proc.wait(timeout=120), stderr


# ---- R1-FIN-04: a reader that closes the pipe early is not an error ------------

@pytest.mark.parametrize("cmd", ["review", "doctor"])
def test_output_command_survives_a_closed_pipe(tmp_path, cmd):
    breached_store(tmp_path / ".tw")
    rc, stderr = closed_reader(cmd, "--dir", str(tmp_path / ".tw"), cwd=tmp_path,
                               read_bytes=200)
    assert "Traceback" not in stderr and "OSError" not in stderr, stderr
    assert rc == 0, stderr


def test_gate_keeps_its_verdict_when_nobody_reads_it(tmp_path):
    # Closing the reader must never turn a breached verdict into exit 0.
    breached_store(tmp_path / ".tw", n=3)
    rc, stderr = closed_reader("gate", "--dir", str(tmp_path / ".tw"), cwd=tmp_path,
                               read_bytes=0)
    assert "Traceback" not in stderr, stderr
    assert rc == 1


# ---- R1-DATA-03: a non-JSON value is named by path, type and conversion --------

class int64:  # stands in for numpy.int64 (numpy is not a test dependency)
    __module__ = "numpy"


class DataFrame:  # stands in for pandas.DataFrame
    __module__ = "pandas.core.frame"


def capture_error(value):
    with pytest.raises(NightwardError) as exc:
        Recorder().add("np_case", value)
    return str(exc.value)


def test_non_json_value_error_names_behavior_path_type_and_fix():
    msg = capture_error({"rows": 10, "units": int64()})
    assert "'np_case'" in msg
    assert "$.units" in msg
    assert "numpy.int64" in msg
    assert ".item()" in msg


@pytest.mark.parametrize("value, path, words", [
    ({"t": [1, datetime.date(2024, 1, 1)]}, "$.t[1]", ["datetime.date", ".isoformat()"]),
    ({"price": decimal.Decimal("1.10")}, "$.price", ["decimal.Decimal", "str("]),
    ({"tags": {"a"}}, "$.tags", ["set", "sorted("]),
    ({"raw": b"x"}, "$.raw", ["bytes", ".decode()"]),
    ({"df": DataFrame()}, "$.df", ["pandas", 'to_dict("records")']),
    ([{"mean": float("nan")}], "$[0].mean", ["nan", "None"]),
    ({"a b": {"x": float("inf")}}, '$["a b"].x', ["inf", "None"]),
])
def test_non_json_value_error_hints_by_type(value, path, words):
    msg = capture_error(value)
    assert path in msg, msg
    for w in words:
        assert w in msg, msg


def test_mixed_dict_key_types_are_named_as_such():
    msg = capture_error({"m": {1: "a", "b": 2}})
    assert "$.m" in msg and "key" in msg and "int" in msg and "str" in msg, msg
    assert "'<' not supported" not in msg


def test_circular_payload_is_named():
    loop: list = []
    loop.append(loop)
    assert "circular" in capture_error({"x": loop})


# ---- R1-DATA-05: review / doctor can be scoped and are capped per behavior -----

def big_change_store(tw):
    """One table-wide change (500 rows) next to two small ones in other groups."""
    store = Store(tw)
    store.ensure()
    rows = [{"id": i, "score": i / 7} for i in range(500)]
    for name, group, old, new in [
        ("scored_rows", "big", rows, [r | {"score": r["score"] + 1} for r in rows]),
        ("metric_a", "m", {"v": 1}, {"v": 2}),
        ("metric_b", "n", {"v": 1}, {"v": 3}),
    ]:
        store.write_pending(Behavior(name=name, payload=old, group=group))
        store.approve(name)
        store.write_pending(Behavior(name=name, payload=new, group=group))
    recompute(store)


def test_review_filters_by_name(tmp_path):
    big_change_store(tmp_path / ".tw")
    r = cli("review", "metric_a", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "metric_a" in r.stdout
    assert "metric_b" not in r.stdout and "scored_rows" not in r.stdout


def test_review_filters_by_group(tmp_path):
    big_change_store(tmp_path / ".tw")
    r = cli("review", "--group", "n", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "metric_b" in r.stdout
    assert "metric_a" not in r.stdout and "scored_rows" not in r.stdout


def test_review_unknown_name_is_a_clean_error(tmp_path):
    big_change_store(tmp_path / ".tw")
    r = cli("review", "metric_zz", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert r.returncode == 2
    assert "metric_zz" in r.stderr and "Traceback" not in r.stderr


def test_review_caps_each_diff_and_says_how_to_see_the_rest(tmp_path):
    big_change_store(tmp_path / ".tw")
    capped = cli("review", "scored_rows", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert capped.returncode == 0, capped.stderr
    assert len(capped.stdout.splitlines()) < 100
    assert "more diff line" in capped.stdout and "--max-lines 0" in capped.stdout
    full = cli("review", "scored_rows", "--max-lines", "0", "--dir", str(tmp_path / ".tw"),
               cwd=tmp_path)
    assert len(full.stdout.splitlines()) > 500
    assert "more diff line" not in full.stdout


def test_doctor_filters_by_name_and_group(tmp_path):
    big_change_store(tmp_path / ".tw")
    by_name = cli("doctor", "metric_a", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert by_name.returncode == 0, by_name.stderr
    assert "metric_a" in by_name.stdout and "metric_b" not in by_name.stdout
    by_group = cli("doctor", "--group", "big", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert "scored_rows" in by_group.stdout and "metric_a" not in by_group.stdout


# ---- R1-WEB-03: a scrub rule that never matches is reported, not silent --------

LOGIN = ('<form method="post">\n'
         '  <input type="hidden" name="csrf_token" value="{tok}">\n</form>')


def test_unmatched_scrub_rules_are_listed():
    scrub.register(r'name="csrf_token" value="[0-9a-f]{32}"', 'x')   # raw-text pattern
    scrub.register(r"ord_\d+", "<ORDER>")
    scrub.register_field("request_id")
    scrub.register_field("never_there")
    scrub.scrub({"order": "ord_1", "request_id": "r1",
                 "body": LOGIN.format(tok="ab" * 16)})
    unmatched = scrub.unmatched_rules()
    assert len(unmatched) == 2
    assert "csrf_token" in unmatched[0] and "never_there" in unmatched[1]


def test_documented_pattern_for_text_inside_a_string_works():
    # README: patterns see the canonical JSON text, where '"' is written \"
    scrub.register(r'csrf_token\\" value=\\"[0-9a-f]{32}', r'csrf_token\\" value=\\"<CSRF>')
    a = scrub.scrub({"body": LOGIN.format(tok="ab" * 16)})
    b = scrub.scrub({"body": LOGIN.format(tok="cd" * 16)})
    assert a == b and "<CSRF>" in a["body"]
    assert scrub.unmatched_rules() == []


def test_run_warns_about_a_scrub_rule_that_matched_nothing(tmp_path):
    write(tmp_path / "conftest.py",
          "from nightward import scrub\n"
          "scrub.register(r'\"request_id\":\"req_[0-9a-f]{12}\"', '\"request_id\":\"<REQ>\"')\n")
    write(tmp_path / "test_e.py",
          "import json\n"
          "def test_e(behavior):\n"
          "    behavior('gw', {'body': json.dumps({'request_id': 'req_0123456789ab'},\n"
          "                                       separators=(',', ':'))})\n")
    r = cli("run", ".", "--dir", ".tw", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "matched nothing" in r.stderr and "request_id" in r.stderr, r.stderr


# ---- R1-WEB-05 / R1-OPS-08: pytest start-up failures name the real cause --------

def test_conftest_import_error_is_not_blamed_on_installation(tmp_path):
    write(tmp_path / "conftest.py", "import not_installed_dependency\n")
    write(tmp_path / "test_a.py", "def test_a(behavior):\n    behavior('a', 1)\n")
    r = cli("run", ".", "--dir", ".tw", cwd=tmp_path)
    assert r.returncode == 2
    assert "nightward is not installed" not in r.stderr, r.stderr
    assert "conftest" in r.stderr and "pytest's output" in r.stderr, r.stderr


def test_conftest_import_error_reaches_the_agent_with_pytests_reason(tmp_path, monkeypatch):
    write(tmp_path / "conftest.py", "import not_installed_dependency\n")
    write(tmp_path / "test_a.py", "def test_a(behavior):\n    behavior('a', 1)\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(NightwardError) as exc:
        execute_run(".", ".tw", capture_output=True)
    assert "not_installed_dependency" in str(exc.value)
    assert "nightward is not installed" not in str(exc.value)
    assert "is nightward installed" not in str(exc.value)


def test_install_hint_only_when_pytest_rejects_the_nightward_options():
    rejected = subprocess.CompletedProcess(
        [], 4, stdout=b"", stderr=b"error: unrecognized arguments: --nightward-record\n")
    assert "nightward is not installed" in runner._abort_message(".", rejected)


def test_missing_run_path_is_named_before_pytest_starts(tmp_path):
    r = cli("run", "tset", "--dir", ".tw", cwd=tmp_path)
    assert r.returncode == 2
    assert "'tset' does not exist" in r.stderr, r.stderr
    assert "nightward is not installed" not in r.stderr
    assert not (tmp_path / ".tw").exists()
