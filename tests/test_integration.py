"""End-to-end: real pytest capture through the CLI gate, in a subprocess.

Exercises the installed plugin entry point and the `nightward` CLI together —
the part unit tests can't reach.
"""
import json
import os
import subprocess
import sys

import pytest

TEST_V1 = '''
def test_alpha(behavior):
    behavior("alpha", {"v": 1}, group="g1")

def test_beta(behavior):
    behavior("beta", {"v": 2}, group="g2")
'''

TEST_V2_CHANGED = '''
def test_alpha(behavior):
    behavior("alpha", {"v": 1}, group="g1")

def test_beta(behavior):
    behavior("beta", {"v": 999}, group="g2")   # changed
'''


def _cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "test_sample.py").write_text(TEST_V1, encoding="utf-8")
    return tmp_path


def test_full_capture_approve_gate_cycle(project):
    tw = project / ".nightward"

    # 1. capture -> everything NEW -> breached
    r = _cli("run", "test_sample.py", "--dir", str(tw), cwd=project)
    assert r.returncode == 0, r.stderr
    assert (tw / "pending" / "alpha.received.json").exists()
    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["boundary"] == "breached"
    assert report["counts"]["new"] == 2

    # 2. approve all -> intact, baseline written
    r = _cli("approve", "--all", "--dir", str(tw), cwd=project)
    assert r.returncode == 0, r.stderr
    assert (tw / "baseline" / "alpha.approved.json").exists()

    # 3. gate passes (exit 0)
    assert _cli("gate", "--dir", str(tw), cwd=project).returncode == 0

    # 4. status --json reports intact
    r = _cli("status", "--dir", str(tw), "--json", cwd=project)
    assert json.loads(r.stdout.strip())["boundary"] == "intact"


def test_change_breaches_boundary_and_gate(project):
    tw = project / ".nightward"
    _cli("run", "test_sample.py", "--dir", str(tw), cwd=project)
    _cli("approve", "--all", "--dir", str(tw), cwd=project)

    # introduce a side effect
    (project / "test_sample.py").write_text(TEST_V2_CHANGED, encoding="utf-8")
    r = _cli("run", "test_sample.py", "--dir", str(tw), cwd=project)
    assert r.returncode == 0, r.stderr

    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["boundary"] == "breached"
    assert report["counts"]["changed"] == 1
    # only the touched behavior is in the blast radius, grouped
    assert "g2" in report["blast_radius"]
    assert report["blast_radius"]["g2"][0]["name"] == "beta"

    # gate now blocks (exit 1) -> this is the agent-loop stop signal
    assert _cli("gate", "--dir", str(tw), cwd=project).returncode == 1


def test_no_tests_collected_errors(tmp_path):
    (tmp_path / "test_empty.py").write_text("# nothing here\n", encoding="utf-8")
    r = _cli("run", "test_empty.py", "--dir", str(tmp_path / ".nightward"), cwd=tmp_path)
    assert r.returncode == 2
    assert "no tests" in r.stderr.lower()


def test_duplicate_name_fails_the_test(tmp_path):
    (tmp_path / "test_dup.py").write_text(
        'def test_d(behavior):\n'
        '    behavior("same", {"v": 1})\n'
        '    behavior("same", {"v": 2})\n',
        encoding="utf-8",
    )
    r = _cli("run", "test_dup.py", "--dir", str(tmp_path / ".nightward"), cwd=tmp_path)
    # pytest reports a failure -> the capture is incomplete: summary, then exit 1
    # (R1-DATA-02: a green exit let CI merge a failing capture)
    assert r.returncode == 1
    assert "capture incomplete" in r.stderr


def test_nonascii_payload_review_survives_legacy_encoding(tmp_path):
    """Hangul in a payload must not crash `review` even under a cp949 console.

    Guards the UnicodeEncodeError regression: rich's legacy Windows writer used
    to blow up on non-ASCII output. We force a cp949 stdout via PYTHONIOENCODING
    (the cp949 codec ships with CPython on every OS, so this runs in CI too).
    """
    (tmp_path / "test_kor.py").write_text(
        'def test_kor(behavior):\n'
        '    behavior("greeting", {"msg": "안녕하세요"}, group="i18n")\n',
        encoding="utf-8",
    )
    tw = tmp_path / ".nightward"
    env = dict(os.environ, PYTHONIOENCODING="cp949")

    assert _cli("run", "test_kor.py", "--dir", str(tw), cwd=tmp_path, env=env).returncode == 0
    _cli("approve", "--all", "--dir", str(tw), cwd=tmp_path, env=env)

    # change the Hangul value so review must print a diff containing Hangul
    (tmp_path / "test_kor.py").write_text(
        'def test_kor(behavior):\n'
        '    behavior("greeting", {"msg": "반갑습니다"}, group="i18n")\n',
        encoding="utf-8",
    )
    run2 = _cli("run", "test_kor.py", "--dir", str(tw), cwd=tmp_path, env=env)
    r = _cli("review", "--dir", str(tw), cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    assert "greeting" in r.stdout, (
        f"review missed the change:\nrun={run2.stdout!r}\nreview={r.stdout!r}"
    )
