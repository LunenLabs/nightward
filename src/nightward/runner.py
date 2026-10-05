"""Shared run logic: capture behaviors via pytest, recompute the blast radius.

Used by both `cli.run` (rich console) and the MCP server (JSON) so the two
surfaces report the same measurement. No console output here.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .core.baseline import Store, digest
from .core.blast import aggregate
from .core.diff import compare
from .errors import NightwardError


def make_judge(spec: str | None, store: Store):
    """Build the semantic judge for a 'provider:model' spec (None -> no judge).

    The verdict ledger lives in the store and is meant to be committed — see
    judge.Judge.equivalent.
    """
    if not spec:
        return None
    from .judge import Judge
    return Judge(spec, cache_path=store.root / "judge_verdicts.json")


def judge_from_meta(store: Store):
    """Recreate the judge the last run used, so approve/recompute see the same
    verdicts (cache makes this free and deterministic)."""
    return make_judge(store.load_run_meta().get("judge"), store)


def recompute(store: Store, judge=None) -> dict:
    """Compare pending against baseline, aggregate, persist, and return the report.

    The report records digests of both inputs so a later reader can tell when
    either moved under it (see is_stale), and whether the capture behind it was
    incomplete (failed/errored tests) - such a report never gates green.
    """
    baseline = store.load_baseline()
    pending = store.load_pending()
    report = aggregate(compare(baseline, pending, judge=judge))
    meta = store.load_run_meta()
    failed, errors = meta.get("failed", 0), meta.get("errors", 0)
    report["incomplete"] = {"failed": failed, "errors": errors} if failed or errors else None
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    report["baseline_digest"] = digest(baseline)
    report["pending_digest"] = digest(pending)
    store.write_report(report)
    return report


def is_stale(store: Store, report: dict | None) -> bool:
    """True when the baseline or the capture changed after `report` was computed.

    Covers `git pull` bringing a new baseline, a direct `pytest --nightward-record`,
    and a run interrupted between capture and report. A report without digests
    can't be checked, so it counts as stale (fail closed). A code edit since the
    last run is NOT detectable - callers that need a fresh verdict must run again.
    """
    if report is None:
        return False
    return (report.get("baseline_digest") != digest(store.load_baseline())
            or report.get("pending_digest") != digest(store.load_pending()))


def _pytest_cmd(path: str, dir: str, run_id: str) -> list[str]:
    # -B: no bytecode cache. Rewriting a test file between runs can otherwise
    # re-import a stale .pyc and silently capture OLD behavior (flaky in CI).
    cmd = [sys.executable, "-B", "-m", "pytest", path,
           "--nightward-record", "--nightward-dir", dir,
           "--nightward-run-id", run_id, "-q"]
    # Capture needs one process: under xdist each worker sees only its share.
    # "-n 0" (last wins) overrides an "-n auto" in the project's addopts.
    if importlib.util.find_spec("xdist") is not None:
        cmd += ["-n", "0"]
    return cmd


# pytest exit codes other than 0 (passed) / 1 (some failed). On these the
# plugin keeps the previous capture, so nothing in the store moves.
_ABORT_REASONS = {
    2: "pytest was interrupted (collection errors or Ctrl+C)",
    3: "pytest hit an internal error",
    4: "pytest rejected the command line (is nightward installed in this "
       "interpreter, so its pytest plugin is registered?)",
    5: "pytest collected no tests",
}


def _output_tail(result: subprocess.CompletedProcess, lines: int = 15) -> str | None:
    """Last lines of pytest's output, or None when it went to the console."""
    output = (result.stderr or b"") + (result.stdout or b"")
    if not output:
        return None
    return "\n".join(output.decode("utf-8", errors="replace").strip().splitlines()[-lines:])


def _abort_message(path: str, result: subprocess.CompletedProcess) -> str:
    reason = _ABORT_REASONS.get(result.returncode,
                                f"pytest exited with code {result.returncode}")
    return _with_tail(f"{reason} under {path!r}; aborting - the store was left untouched",
                      result)


def _with_tail(msg: str, result: subprocess.CompletedProcess) -> str:
    # With captured output the user never saw pytest's own explanation.
    tail = _output_tail(result)
    if tail:
        msg += "\n--- pytest output (tail) ---\n" + tail
    return msg


def execute_run(path: str = ".", dir: str = ".nightward", *,
                capture_output: bool = False, judge_spec: str | None = None,
                timeout: float | None = None) -> dict:
    """Run pytest in a subprocess to capture behaviors, then recompute.

    capture_output=True keeps pytest's stdout off this process's stdout — required
    when called from the MCP stdio server (any stray stdout breaks the protocol).
    judge_spec ('provider:model', or env NIGHTWARD_JUDGE) enables the semantic
    judge for behaviors captured with semantic=True; the spec is persisted in
    run_meta so later approve/recompute reuse the same (cached) verdicts.
    timeout (seconds) bounds the pytest run; on expiry nothing in the store moves.
    Returns {report, skipped, failed, errors, deselected, xfailed, scrubbed,
    pytest_returncode, output_tail};
    output_tail is pytest's last lines when capture_output=True, so a caller can
    see why tests failed.
    """
    spec = judge_spec or os.environ.get("NIGHTWARD_JUDGE") or None
    run_id = uuid.uuid4().hex
    try:
        # stdin=DEVNULL: under `nightward mcp` our stdin is the protocol pipe; a
        # child inheriting it hangs on Windows while the server reads it.
        result = subprocess.run(_pytest_cmd(path, dir, run_id), stdin=subprocess.DEVNULL,
                                capture_output=capture_output, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise NightwardError(
            f"pytest timed out after {timeout}s under {path!r}; the store was left untouched"
        ) from exc
    if result.returncode not in (0, 1):
        raise NightwardError(_abort_message(path, result))
    store = Store(Path(dir))
    meta = store.load_run_meta()
    if meta.get("run_id") != run_id:
        # Exit 1 is also what a crash inside the plugin's flush looks like. Then
        # pending/ still holds the PREVIOUS capture - comparing it would replay
        # an old verdict. Invalidate the report so nothing reads "intact".
        store.invalidate_report()
        msg = (f"this run's capture was not recorded under {path!r} (pytest did not "
               f"finish writing it - see pytest's error output); the last report was "
               f"invalidated. Fix the error and re-run `nightward run`.")
        raise NightwardError(_with_tail(msg, result))
    if spec:
        meta["judge"] = spec
    else:
        meta.pop("judge", None)
    store.write_run_meta(meta)
    report = recompute(store, judge=make_judge(spec, store))
    return {
        "report": report,
        "skipped": meta.get("skipped", 0),
        "failed": meta.get("failed", 0),
        "errors": meta.get("errors", 0),
        "deselected": meta.get("deselected", 0),
        "xfailed": meta.get("xfailed", 0),
        # values the built-in timestamp/uuid scrubbers masked: {values, behaviors}
        "scrubbed": meta.get("scrubbed") or {"values": 0, "behaviors": 0},
        "pytest_returncode": result.returncode,
        "output_tail": _output_tail(result),
    }
