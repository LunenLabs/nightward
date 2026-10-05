"""Hardening round 2: defects found by a full code review, frozen as tests.

Each test reproduces a failure that existed before the fix (traceback, silent
data loss, or a write outside the store). A failure here is a real defect —
fix the code, do not weaken the test.
"""
import json
import socket
import subprocess
import sys

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, validate_name
from nightward.errors import NightwardError
from nightward.judge import Judge
from nightward.pytest_plugin import Recorder
from nightward.runner import execute_run
from nightward.scrub import scrub


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def report_of(store_dir):
    return json.loads((store_dir / "report.json").read_text(encoding="utf-8"))


# R1: rich parses markup, so a parametrized-style name vanished from the output
# and a payload containing "[/x]" crashed `review` with a MarkupError.
def test_r1_captured_markup_is_printed_literally(tmp_path):
    write(tmp_path / "test_s.py",
          'def test_s(behavior):\n'
          '    behavior("total[eur]", {"note": "[/x] [bold]hi"}, group="g[1]")\n')
    run = cli("run", "test_s.py", cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    assert "total[eur]" in run.stdout
    assert "g[1]" in run.stdout

    review = cli("review", cwd=tmp_path)
    assert review.returncode == 0, review.stderr
    assert "Traceback" not in review.stderr
    assert "[/x] [bold]hi" in review.stdout


# R2: names differing only in case are one file on Windows/macOS -> one
# behavior silently overwrote the other.
def test_r2_case_insensitive_name_collision_rejected():
    rec = Recorder()
    rec.add("Total", 1)
    with pytest.raises(NightwardError, match="collides"):
        rec.add("total", 2)


def test_r2_exact_duplicate_still_reported_as_duplicate():
    rec = Recorder()
    rec.add("total", 1)
    with pytest.raises(NightwardError, match="duplicate"):
        rec.add("total", 2)


# R3: Windows device names can't be created as files there.
@pytest.mark.parametrize("bad", ["CON", "nul", "com1", "LPT9", "aux.v2"])
def test_r3_windows_reserved_names_rejected(bad):
    with pytest.raises(NightwardError, match="reserved"):
        validate_name(bad)


@pytest.mark.parametrize("ok", ["console", "nullable", "com10", "aux_total"])
def test_r3_lookalike_names_still_valid(ok):
    assert validate_name(ok) == ok


# R4: `reject ../../x` wrote outside the store, and rejecting a name that
# doesn't exist "succeeded" with an empty audit record.
def test_r4_reject_cannot_escape_the_store(tmp_path):
    r = cli("reject", "../../evil", cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert not list(tmp_path.rglob("evil*"))
    assert not list(tmp_path.parent.glob("evil*"))


def test_r4_reject_unknown_name_is_an_error(tmp_path):
    r = cli("reject", "ghost", cwd=tmp_path)
    assert r.returncode == 2
    assert "ghost" in r.stderr


def test_r4_reject_removed_behavior_records_the_approved_snapshot(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.write_pending(Behavior("gone", {"v": 1}))
    store.approve("gone")
    store.clear_pending()
    store.mark_rejected("gone")
    recorded = json.loads(
        (store.rejected_dir / "gone.rejected.json").read_text(encoding="utf-8"))
    assert recorded["payload"] == {"v": 1}


# R5: a store file that is valid JSON of the wrong shape (or not UTF-8) was a
# raw traceback instead of a clean error.
@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b'{"payload": 1}', b"\xff\xfe\x00"])
def test_r5_malformed_behavior_file_is_a_clean_error(tmp_path, body):
    store = Store(tmp_path)
    store.ensure()
    (store.baseline_dir / "x.approved.json").write_bytes(body)
    with pytest.raises(NightwardError, match="corrupt"):
        store.load_baseline()


def test_r5_malformed_behavior_file_via_cli(tmp_path):
    tw = tmp_path / ".nightward"
    (tw / "baseline").mkdir(parents=True)
    write(tw / "baseline" / "x.approved.json", "[1, 2]")
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr


def test_r5_non_object_report_and_meta(tmp_path):
    store = Store(tmp_path)
    write(tmp_path / "report.json", "[]")
    with pytest.raises(NightwardError, match="corrupt"):
        store.load_report()
    write(tmp_path / "run_meta.json", "[]")
    assert store.load_run_meta() == {}  # advisory: treated as absent


# R6: `init --dir X` still wrote ignore rules for .nightward/.
def test_r6_init_gitignore_follows_dir(tmp_path):
    r = cli("init", "--dir", "build/nw", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "build/nw/pending/" in gi
    assert "build/nw/run_meta.json" in gi
    assert not any(line.startswith(".nightward/") for line in gi)


def test_r6_init_default_dir_lines_unchanged(tmp_path):
    cli("init", cwd=tmp_path)
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert gi[-6:] == [".nightward/pending/", ".nightward/rejected/",
                       ".nightward/report.json", ".nightward/run_meta.json",
                       ".nightward/pending.tmp/", ".nightward/**/*.tmp"]


def test_r6_init_store_outside_cwd_is_not_ignored_here(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    r = cli("init", "--dir", str(tmp_path / "elsewhere"), cwd=project)
    assert r.returncode == 0, r.stderr
    assert not (project / ".gitignore").exists()
    assert "outside" in r.stdout


# R7: text scrubbing could collapse distinct keys into one ("<TIMESTAMP>"),
# silently dropping values — a change behind a dropped key passed the gate.
def test_r7_scrub_key_collision_fails_loudly():
    with pytest.raises(NightwardError, match="collapsed distinct dict keys"):
        scrub({"2024-01-01T00:00:00": 1, "2024-01-02T00:00:00": 2})


def test_r7_single_scrubbed_key_is_fine():
    assert scrub({"2024-01-01T00:00:00": 1}) == {"<TIMESTAMP>": 1}


# R8: an interrupted session (collection error) flushed an EMPTY capture into
# pending — `approve --all` afterwards would have wiped the whole baseline.
def test_r8_aborted_run_keeps_previous_capture(tmp_path):
    write(tmp_path / "test_a.py",
          'def test_a(behavior):\n    behavior("a", 1)\n'
          'def test_b(behavior):\n    behavior("b", 2)\n')
    assert cli("run", ".", cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    pending = tmp_path / ".nightward" / "pending"
    before = sorted(p.name for p in pending.iterdir())

    write(tmp_path / "test_z.py", "def broken(:\n")
    r = cli("run", ".", cwd=tmp_path)
    assert r.returncode == 2
    assert "interrupted" in r.stderr
    assert sorted(p.name for p in pending.iterdir()) == before

    cli("approve", "--all", cwd=tmp_path)
    baseline = tmp_path / ".nightward" / "baseline"
    assert sorted(p.name for p in baseline.iterdir()) == ["a.approved.json",
                                                          "b.approved.json"]


def test_r8_captured_abort_message_carries_pytest_output(tmp_path):
    write(tmp_path / "test_z.py", "def broken(:\n")
    with pytest.raises(NightwardError, match="SyntaxError|invalid syntax"):
        execute_run(str(tmp_path), str(tmp_path / ".nightward"), capture_output=True)


# R9: a corrupt committed ledger (e.g. merge-conflict markers) was silently
# replaced by an empty one on the next save, destroying recorded rulings.
def test_r9_corrupt_ledger_fails_loudly_and_is_preserved(tmp_path):
    ledger = tmp_path / "judge_verdicts.json"
    write(ledger, "<<<<<<< HEAD\n")
    with pytest.raises(NightwardError, match="corrupt judge verdict ledger"):
        Judge("persona:lenient", cache_path=ledger)
    assert ledger.read_text(encoding="utf-8") == "<<<<<<< HEAD\n"


def test_r9_hand_edited_entry_without_reason_still_replays(tmp_path):
    # A model ruling (persona rulings re-judge under new rules: R1-FIN-03).
    ledger = tmp_path / "judge_verdicts.json"
    spec = "anthropic:claude-haiku-4-5"
    write(ledger, json.dumps({f"f1:f2:{spec}": {"verdict": "SAME"}}))
    v = Judge(spec, cache_path=ledger).equivalent("a", "b", "f1", "f2")
    assert v.verdict == "SAME" and v.cached


# R10: `approve --all` ignored the judge, so it re-anchored judged-SAME
# rewordings the report itself listed as unchanged.
def test_r10_approve_all_matches_the_report(tmp_path):
    test_py = tmp_path / "test_ai.py"
    body = ('def test_ai(behavior):\n'
            '    behavior("summary", {text!r}, semantic=True)\n'
            '    behavior("count", {count})\n')
    write(test_py, body.format(text="market went up", count=1))
    cli("run", ".", cwd=tmp_path)
    cli("approve", "--all", cwd=tmp_path)

    write(test_py, body.format(text="the market rose", count=2))
    cli("run", ".", "--judge", "persona:lenient", cwd=tmp_path)
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "count" in r.stdout and "summary" not in r.stdout
    approved = json.loads((tmp_path / ".nightward" / "baseline" / "summary.approved.json")
                          .read_text(encoding="utf-8"))
    assert approved["payload"] == "market went up"  # original anchor kept
    assert report_of(tmp_path / ".nightward")["boundary"] == "intact"


def test_r10_approve_name_and_all_together_is_an_error(tmp_path):
    r = cli("approve", "x", "--all", cwd=tmp_path)
    assert r.returncode == 2
    assert "not both" in r.stderr


# R11: `view` on a busy port died with a raw OSError traceback.
def test_r11_view_busy_port_is_a_clean_error(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        r = cli("view", "--port", str(port), "--no-open", "--out", str(tmp_path / "site"),
                cwd=tmp_path)
    assert r.returncode == 2
    assert "Traceback" not in r.stderr
    assert "--port" in r.stderr


# R12: read-only openpyxl workbooks keep the file handle open until closed,
# which locks the file on Windows.
def test_r12_from_xlsx_releases_the_file(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from nightward.adapters import from_xlsx

    path = tmp_path / "book.xlsx"
    wb = openpyxl.Workbook()
    wb.active["A1"] = 1
    wb.save(path)
    from_xlsx(path)
    path.rename(tmp_path / "moved.xlsx")  # PermissionError on Windows if still open
