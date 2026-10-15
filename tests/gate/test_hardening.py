"""Adversarial hardening cases beyond the persona scenarios.

This file grows every time we find a new edge. A failure here is a real defect.
"""
import json
import os
import subprocess
import sys

import pytest

from nightward.core.behavior import Behavior, validate_name
from nightward.core.blast import aggregate
from nightward.core.diff import compare
from nightward.errors import NightwardError


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


# H1: running an unchanged project repeatedly never drifts off intact.
def test_h1_double_run_idempotent(tmp_path):
    write(tmp_path / "test_s.py", 'def test_s(behavior):\n    behavior("s", {"v": 1})\n')
    tw = tmp_path / ".tw"
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    cli("review", "--dir", str(tw), cwd=tmp_path)   # D26: only review marks
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["boundary"] == "intact"


# H2: an intentionally removed behavior can be approved away, restoring intact.
def test_h2_removal_approval_via_cli(tmp_path):
    write(tmp_path / "test_s.py",
          'def test_a(behavior):\n    behavior("a", {"v": 1})\n'
          'def test_b(behavior):\n    behavior("b", {"v": 2})\n')
    tw = tmp_path / ".tw"
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    cli("review", "--dir", str(tw), cwd=tmp_path)   # D26: only review marks
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)

    # drop behavior "b"
    write(tmp_path / "test_s.py", 'def test_a(behavior):\n    behavior("a", {"v": 1})\n')
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["counts"]["removed"] == 1

    cli("review", "--dir", str(tw), cwd=tmp_path)  # D26: only review marks
    r = cli("approve", "b", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tw / "baseline" / "b.approved.json").exists()
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0


# H3: falsy / empty payloads are valid and stable, not silently mishandled.
@pytest.mark.parametrize("val", [None, 0, False, "", [], {}, 0.0])
def test_h3_falsy_payloads_stable(val):
    a, b = Behavior("x", val), Behavior("x", val)
    assert a.fingerprint() == b.fingerprint()
    assert aggregate(compare({"x": a}, {"x": b}))["boundary"] == "intact"


# H5: a Hangul group name survives capture, report, and review under cp949.
def test_h5_unicode_group_survives(tmp_path):
    write(tmp_path / "test_g.py",
          'def test_g(behavior):\n    behavior("g", {"v": 1}, group="결제")\n')
    tw = tmp_path / ".tw"
    env = dict(os.environ, PYTHONIOENCODING="cp949")
    cli("run", "test_g.py", "--dir", str(tw), cwd=tmp_path, env=env)
    cli("review", "--dir", str(tw), cwd=tmp_path)   # D26: only review marks
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path, env=env)
    write(tmp_path / "test_g.py",
          'def test_g(behavior):\n    behavior("g", {"v": 2}, group="결제")\n')
    cli("run", "test_g.py", "--dir", str(tw), cwd=tmp_path, env=env)

    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert "결제" in report["blast_radius"], f"unicode group lost; report={report}"
    r = cli("review", "--dir", str(tw), cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr


# H6: a corrupt report.json must produce a clean error, never a traceback.
def test_h6_corrupt_report_no_traceback(tmp_path):
    tw = tmp_path / ".tw"
    tw.mkdir()
    (tw / "report.json").write_text("{ this is not json", encoding="utf-8")
    for cmd in ("status", "gate", "review"):
        r = cli(cmd, "--dir", str(tw), cwd=tmp_path)
        assert "Traceback" not in r.stderr, f"{cmd} leaked a traceback"
        assert r.returncode == 2, f"{cmd} should exit 2 on corrupt report"


# H7: --dir pointing at a regular file must fail cleanly, not crash inside pytest.
def test_h7_run_dir_is_a_file(tmp_path):
    notadir = tmp_path / "notadir"
    notadir.write_text("x", encoding="utf-8")
    write(tmp_path / "test_s.py", 'def test_s(behavior):\n    behavior("s", {"v": 1})\n')
    r = cli("run", "test_s.py", "--dir", str(notadir), cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr


# H8: NaN / Infinity are not portable JSON; snapshots must reject them, not write garbage.
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_h8_nan_inf_rejected(bad):
    with pytest.raises(NightwardError):
        Behavior("x", {"v": bad}).fingerprint()


# H9: an absurdly long name would overflow filename limits; cap it with a clear error.
def test_h9_overlong_name_rejected():
    with pytest.raises(NightwardError):
        validate_name("a" * 300)


# H10: a circular reference must be a clean error, not a crash; deep nesting must work.
def test_h10_circular_reference_clear_error():
    d = {}
    d["self"] = d
    with pytest.raises(NightwardError):
        Behavior("x", d).fingerprint()


def test_h10_deep_nesting_serializes():
    root = cur = {}
    for _ in range(200):
        cur["n"] = {}
        cur = cur["n"]
    Behavior("x", root).fingerprint()  # must not raise


# H11: a skipped test silently drops its behavior -> false REMOVED. We must warn.
def test_h11_skipped_test_warns(tmp_path):
    write(tmp_path / "test_s.py",
          'def test_a(behavior):\n    behavior("a", {"v": 1})\n'
          'def test_b(behavior):\n    behavior("b", {"v": 2})\n')
    tw = tmp_path / ".tw"
    cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)
    cli("review", "--dir", str(tw), cwd=tmp_path)   # D26: only review marks
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)

    # behavior "b" is now conditionally skipped, not removed on purpose
    write(tmp_path / "test_s.py",
          'import pytest\n'
          'def test_a(behavior):\n    behavior("a", {"v": 1})\n'
          '@pytest.mark.skip(reason="conditionally off")\n'
          'def test_b(behavior):\n    behavior("b", {"v": 2})\n')
    r = cli("run", "test_s.py", "--dir", str(tw), cwd=tmp_path)

    assert r.returncode == 0  # skips do not fail the run
    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["counts"]["removed"] == 1  # the false REMOVED we want surfaced
    assert "skip" in r.stderr.lower(), "must warn that skips may cause spurious REMOVED"
