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

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.errors import NightwardError
from nightward.pytest_plugin import Recorder
from nightward.runner import recompute


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
