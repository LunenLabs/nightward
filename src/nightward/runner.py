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
from pathlib import Path

from .core.baseline import Store, baseline_digest
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
    """Compare pending against baseline, aggregate, persist, and return the report."""
    baseline = store.load_baseline()
    report = aggregate(compare(baseline, store.load_pending(), judge=judge))
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    report["baseline_digest"] = baseline_digest(baseline)
    if judge is not None:
        report["judge"] = judge.summary()
    store.write_report(report)
    return report


def is_stale(store: Store, report: dict | None) -> bool:
    """True when the baseline changed after `report` was computed.

    Only baseline drift is detectable; a code edit since the last run is not -
    callers that need a fresh verdict must run again.
    """
    digest = (report or {}).get("baseline_digest")
    return digest is not None and digest != baseline_digest(store.load_baseline())


def _pytest_cmd(path: str, dir: str) -> list[str]:
    # -B: no bytecode cache. Rewriting a test file between runs can otherwise
    # re-import a stale .pyc and silently capture OLD behavior (flaky in CI).
    cmd = [sys.executable, "-B", "-m", "pytest", path,
           "--nightward-record", "--nightward-dir", dir, "-q"]
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
    msg = f"{reason} under {path!r}; aborting - the store was left untouched"
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
    Returns {report, skipped, failed, pytest_returncode, output_tail}; output_tail
    is pytest's last lines when capture_output=True, so a caller can see why
    tests failed.
    """
    spec = judge_spec or os.environ.get("NIGHTWARD_JUDGE") or None
    store = Store(Path(dir))
    # Build (= validate) the judge before pytest: a typo'd spec or a corrupt
    # ledger must fail in a second, not after the whole suite (R1-LLM-06).
    judge = make_judge(spec, store)
    try:
        result = subprocess.run(_pytest_cmd(path, dir), capture_output=capture_output,
                                timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise NightwardError(
            f"pytest timed out after {timeout}s under {path!r}; the store was left untouched"
        ) from exc
    if result.returncode not in (0, 1):
        raise NightwardError(_abort_message(path, result))
    meta = store.load_run_meta()
    report = recompute(store, judge=judge)
    # Persist the spec only once it has judged this run, so approve reuses it.
    if spec:
        meta["judge"] = spec
    else:
        meta.pop("judge", None)
    store.write_run_meta(meta)
    return {
        "report": report,
        "skipped": meta.get("skipped", 0),
        "failed": meta.get("failed", 0),
        "pytest_returncode": result.returncode,
        "output_tail": _output_tail(result),
    }
