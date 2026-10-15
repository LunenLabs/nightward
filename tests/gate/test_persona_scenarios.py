"""Persona-driven scenarios.

Each persona stresses a different corner of nightward. Several deliberately probe
edges that plain unit tests miss; if one fails, that is a real bug to fix, not a
test to weaken.
"""
import json
import subprocess
import sys

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, validate_name
from nightward.core.blast import aggregate
from nightward.core.diff import compare
from nightward.errors import NightwardError
from nightward.scrub import register, scrub


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


# --- P1 solo AI dev: a ralph loop polls status on a virgin dir, must not crash ---
def test_p1_status_on_uninitialized_dir(tmp_path):
    r = cli("status", "--dir", str(tmp_path / ".tw"), "--json", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip())["boundary"] == "unknown"


# --- P2 CI engineer: gate with no prior report must fail clearly, not silently breach ---
def test_p2_gate_without_report_errors_clearly(tmp_path):
    r = cli("gate", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert r.returncode == 2
    assert "run" in (r.stdout + r.stderr).lower()
    assert "Traceback" not in r.stderr


# --- P3 ML engineer: nested floats stay stable across identical runs ---
def test_p3_nested_floats_stable():
    payload = {"weights": [0.1, 0.2, 0.1 + 0.2], "meta": {"lr": 1e-4}}
    a, b = Behavior("m", payload), Behavior("m", dict(payload))
    assert a.fingerprint() == b.fingerprint()
    assert aggregate(compare({"m": a}, {"m": b}))["boundary"] == "intact"


# --- P4 API backend: ISO timestamp + uuid scrubbed -> stable across runs ---
def test_p4_api_response_scrubbed():
    r1 = scrub({"id": "12345678-1234-1234-1234-123456789abc",
                "at": "2026-06-05T10:00:00Z", "ok": True})
    r2 = scrub({"id": "abcdef00-0000-0000-0000-000000000000",
                "at": "2030-01-01T00:00:00Z", "ok": True})
    assert r1 == r2


# --- P5 API backend extension: a quoted scrubber works; a JSON-breaking one fails loudly ---
def test_p5_custom_scrubber_quoted_ok():
    register(r'"\d{10}"', '"<EPOCH>"')
    assert scrub({"ts": "1700000000"}) == {"ts": "<EPOCH>"}


def test_p5_scrubber_breaking_json_raises():
    register(r"(?<=: )\d{10}", "<EPOCH>")  # unquoted -> invalid JSON
    with pytest.raises(NightwardError):
        scrub({"ts": 1700000000})


# --- P6 Korean dev: Hangul behavior names must be allowed (target audience!) ---
def test_p6_hangul_behavior_name_allowed():
    assert validate_name("주문합계") == "주문합계"


def test_p6_hangul_name_roundtrips_through_store(tmp_path):
    store = Store(tmp_path)
    store.ensure()
    store.write_pending(Behavior("결제내역", {"won": 12000}))
    store.approve("결제내역")
    assert "결제내역" in store.load_baseline()


# --- P7 legacy characterization: a non-serializable capture surfaces a clear error ---
def test_p7_non_serializable_clear_error():
    with pytest.raises(NightwardError) as exc:
        scrub({"obj": object()})
    assert "serializ" in str(exc.value).lower()


# --- P8 monorepo: 50 behaviors / 5 groups; change 3 -> blast radius isolates exactly those ---
def test_p8_monorepo_blast_isolation(tmp_path):
    body = ""
    for i in range(50):
        body += (f'def test_b{i}(behavior):\n'
                 f'    behavior("beh_{i}", {{"v": {i}}}, group="mod{i % 5}")\n')
    write(tmp_path / "test_many.py", body)
    tw = tmp_path / ".tw"

    assert cli("run", "test_many.py", "--dir", str(tw), cwd=tmp_path).returncode == 0
    cli("review", "--dir", str(tw), cwd=tmp_path)   # D26: only review marks
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)

    changed = (body.replace('"v": 0}', '"v": 999}')
                   .replace('"v": 7}', '"v": 999}')
                   .replace('"v": 13}', '"v": 999}'))
    write(tmp_path / "test_many.py", changed)
    cli("run", "test_many.py", "--dir", str(tw), cwd=tmp_path)

    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["counts"]["changed"] == 3
    assert report["counts"]["unchanged"] == 47


# --- P9 newcomer: friendly errors, never tracebacks ---
def test_p9_review_before_run(tmp_path):
    r = cli("review", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr


def test_p9_approve_ghost(tmp_path):
    r = cli("approve", "ghost", "--dir", str(tmp_path / ".tw"), cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in (r.stdout + r.stderr)


# --- P10 Windows/paths: a nested non-existent --dir is created; Hangul payload survives ---
def test_p10_nested_dir_created_with_hangul_payload(tmp_path):
    write(tmp_path / "test_x.py",
          'def test_x(behavior):\n    behavior("x", {"msg": "안녕하세요"})\n')
    deep = tmp_path / "a" / "b" / "c" / ".tw"
    r = cli("run", "test_x.py", "--dir", str(deep), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (deep / "pending" / "x.received.json").exists()
