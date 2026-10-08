"""Shared run logic: capture behaviors via pytest, recompute the blast radius.

Used by both `cli.run` (rich console) and the MCP server (JSON) so the two
surfaces report the same measurement. No console output here.
"""
from __future__ import annotations

import datetime
import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .core.baseline import Store, change_token, digest
from .core.blast import aggregate
from .core.diff import CHANGED, NOT_RUN, REMOVED, UNCHANGED, compare
from .core.lock import store_lock
from .errors import NightwardError


def store_above(dir_: str) -> str | None:
    """A store named `dir_` in a parent directory (cwd is a subdirectory of the
    project): running there would silently create a second, empty store."""
    if Path(dir_).is_absolute():
        return None
    for parent in Path.cwd().parents:
        if (parent / dir_).is_dir():
            return os.path.relpath(parent / dir_)
    return None


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


def standing_rejections(store: Store, baseline, pending) -> dict[str, str]:
    """Names whose current state is exactly what someone rejected -> who (or "").

    The record is the received behavior, or the approved one for a rejected
    removal; a later, different payload is a new change and is not held.
    """
    held = {}
    for name, rec in store.load_rejected().items():
        current = pending.get(name) or baseline.get(name)
        if current is not None and (current.fingerprint(), current.group) == (
                rec.fingerprint(), rec.group):
            held[name] = store.rejected_by(name) or ""
    return held


def classify(store: Store, baseline, pending, judge=None, *, with_diff: bool = True):
    """compare(), plus the human decisions the store holds (D19).

    A standing rejection is part of what a reviewer must see, and it beats
    the judge: a judged-SAME rewording someone rejected, or an approved
    baseline that is exactly a rejected payload (a merge brought both), is
    unapproved again until the code is fixed or `approve NAME` overrides it.
    """
    changes = compare(baseline, pending, judge=judge, with_diff=with_diff)
    rejected = standing_rejections(store, baseline, pending)
    deselected = set(store.load_run_meta().get("deselected_ids") or ())
    for c in changes:
        c.token = change_token(baseline.get(c.name), pending.get(c.name))
        source = baseline[c.name].source if c.name in baseline else None
        if c.kind == REMOVED and source in deselected and c.name not in rejected:
            # The user narrowed the run with -k/-m: not checked, not removed (D21).
            # Skips and source-less baselines stay REMOVED (fail closed).
            c.kind, c.diff_text = NOT_RUN, ""
            continue
        if c.name not in rejected:
            continue
        c.rejected, c.rejected_by = True, rejected[c.name]
        if c.kind != UNCHANGED:
            continue
        who = f" by {c.rejected_by}" if c.rejected_by else ""
        if c.judged:
            why = (f"the judge ({c.judge_model}) ruled this SAME, but it was rejected{who} "
                   f"- a human rejection overrules the judge")
            c.judged = False
        else:
            why = (f"the approved baseline is exactly the payload rejected{who} "
                   f"(rejected/{c.name}.rejected.json) - fix the code, or approve it by "
                   f"name to clear the rejection")
        c.kind = CHANGED
        c.diff_text = why + ("\n" + c.diff_text if c.diff_text else "")
    return changes


NO_JUDGE = ("no judge configured - set [tool.nightward] judge in the project's "
            "pyproject.toml (`nightward run --judge` applies to one CLI run only)")


def recompute(store: Store, judge=None, *, baseline=None, pending=None) -> dict:
    """Compare pending against baseline, aggregate, persist, and return the report.

    The report records digests of both inputs so a later reader can tell when
    either moved under it (see is_stale), and whether the capture behind it was
    incomplete (failed/errored tests) - such a report never gates green.
    baseline/pending: already-loaded inputs (saves reading the store again).
    """
    baseline = store.load_baseline() if baseline is None else baseline
    pending = store.load_pending() if pending is None else pending
    changes = classify(store, baseline, pending, judge=judge)
    report = aggregate(changes)
    meta = store.load_run_meta()
    failed, errors = meta.get("failed", 0), meta.get("errors", 0)
    report["incomplete"] = {"failed": failed, "errors": errors} if failed or errors else None
    report["narrowed"] = bool(meta.get("narrowed"))
    report["generated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")
    report["baseline_digest"] = digest(baseline)
    report["pending_digest"] = digest(pending)
    if judge is not None:
        report["judge"] = judge.summary()
    elif unjudged := [c.name for c in changes if c.kind == CHANGED
                      and baseline[c.name].semantic and pending[c.name].semantic
                      and baseline[c.name].fingerprint() != pending[c.name].fingerprint()]:
        # Approved as semantic, but nothing could judge them: say why they
        # breach on a rewording, as an unavailable judge does (R3-FIN-08).
        report["judge"] = {"spec": None, "unavailable": NO_JUDGE,
                           "compared_exactly": unjudged}
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


def recompute_capture(store: Store) -> dict:
    """The verdict for the capture already in pending/, without running pytest.

    For CI that captures with `pytest --nightward-record` in its own test job
    (R2-DATA-04). Trusted only when run_meta proves pending/ is exactly what a
    complete pytest session flushed (the plugin records its digest), so a
    hand-edited or half-written capture never becomes a verdict.
    """
    with store_lock(store.root, "nightward report"):
        meta = store.load_run_meta()
        pending = store.load_pending()
        if not meta.get("pending_digest"):
            raise NightwardError(
                f"no recorded capture session in {store.root} - capture first with "
                f"`pytest --nightward-record` (or `nightward run`)")
        if meta["pending_digest"] != digest(pending):
            raise NightwardError(
                f"{store.pending_dir} does not match the last recorded capture session "
                f"(edited by hand, or written by an older nightward) - capture again with "
                f"`pytest --nightward-record` or `nightward run`")
        return recompute(store, judge=judge_from_meta(store), pending=pending)


def _pytest_cmd(path: str, dir: str, run_id: str, extra: list[str]) -> list[str]:
    # -B: no bytecode cache. Rewriting a test file between runs can otherwise
    # re-import a stale .pyc and silently capture OLD behavior (flaky in CI).
    # The user's args go first so ours (and "-n 0") win.
    cmd = [sys.executable, "-B", "-m", "pytest", path, *extra,
           "--nightward-record", "--nightward-dir", dir,
           "--nightward-run-id", run_id, "-q"]
    # Capture needs one process: under xdist each worker sees only its share.
    # "-n 0" (last wins) overrides an "-n auto" in the project's addopts.
    if importlib.util.find_spec("xdist") is not None:
        cmd += ["-n", "0"]
    return cmd


# pytest exit codes other than 0 (passed) / 1 (some failed). On these the
# plugin keeps the previous capture, and the runner invalidates the report:
# the code no longer matches it, so nothing may read "intact" (D12).
_ABORT_REASONS = {
    2: "pytest was interrupted (collection errors or Ctrl+C)",
    3: "pytest hit an internal error",
    4: "pytest could not load the test suite (a conftest.py or plugin failed to "
       "import, or the command line was rejected - see pytest's output)",
    5: "pytest collected no tests",
}


def _output_tail(result: subprocess.CompletedProcess, lines: int = 15) -> str | None:
    """Last lines of pytest's output, or None when it went to the console."""
    output = (result.stderr or b"") + (result.stdout or b"")
    if not output:
        return None
    return "\n".join(output.decode("utf-8", errors="replace").strip().splitlines()[-lines:])


def _plugin_missing(result: subprocess.CompletedProcess) -> bool:
    """True when pytest ran without nightward's plugin (so it rejects our options)."""
    tail = _output_tail(result) or ""
    if "unrecognized arguments" in tail and "--nightward" in tail:
        return True
    return not any(ep.value == "nightward.pytest_plugin"
                   for ep in importlib.metadata.entry_points(group="pytest11"))


def _abort_message(path: str, result: subprocess.CompletedProcess) -> str:
    reason = _ABORT_REASONS.get(result.returncode,
                                f"pytest exited with code {result.returncode}")
    if result.returncode == 4 and _plugin_missing(result):
        # Only now: most exit-4s are a conftest import error (R1-WEB-05).
        reason = ("pytest rejected the --nightward-* options: nightward is not installed "
                  "in this interpreter, so its pytest plugin is not registered "
                  "(pip install nightward)")
    return _with_tail(f"{reason} under {path!r}; aborting - {_ABORTED}", result)


# What an unverified run leaves behind (R2-OPS-01): the old capture stays, but
# its report no longer describes the code, so gate/status fall back to "no report".
_ABORTED = ("the capture was left untouched and the last report was invalidated; fix "
            "the error and re-run `nightward run`")


def _with_tail(msg: str, result: subprocess.CompletedProcess) -> str:
    # With captured output the user never saw pytest's own explanation.
    tail = _output_tail(result)
    if tail:
        msg += "\n--- pytest output (tail) ---\n" + tail
    return msg


@contextmanager
def refused_run_invalidates(dir: str | Path) -> Iterator[None]:
    """A run refused or aborted anywhere - judge config, ledger, busy lock,
    pytest - leaves no verdict behind: the old report no longer describes
    the code, so gate/status must not read it as current (D12, D19)."""
    try:
        yield
    except NightwardError:
        Store(Path(dir)).invalidate_report()
        raise


def execute_run(path: str = ".", dir: str = ".nightward", *,
                capture_output: bool = False, judge_spec: str | None = None,
                timeout: float | None = None, command: str = "nightward run",
                pytest_args: list[str] | None = None) -> dict:
    """Run pytest in a subprocess to capture behaviors, then recompute.

    capture_output=True keeps pytest's stdout off this process's stdout — required
    when called from the MCP stdio server (any stray stdout breaks the protocol).
    judge_spec ('provider:model') enables the semantic judge for behaviors
    approved with semantic=True. Callers resolve it (cli.run: --judge, env,
    [tool.nightward]; MCP: server --judge, [tool.nightward]); it is persisted
    in run_meta only so approve recomputes with this run's (cached) verdicts.
    timeout (seconds) bounds the pytest run. Any run without a verified capture
    (refused before pytest, abort, timeout, unrecorded flush) invalidates
    report.json. command names
    the holder in the store lock; a concurrent writer fails with NightwardError.
    pytest_args are passed to pytest as-is (-m, -p, --timeout ...); a narrowed
    run is recorded as such and never proves a removal (D13).
    Returns {report, skipped, failed, errors, deselected, xfailed, scrubbed,
    scrub_unmatched, pytest_returncode, output_tail};
    output_tail is pytest's last lines when capture_output=True, so a caller can
    see why tests failed.
    """
    with refused_run_invalidates(dir):
        return _execute_run(path, dir, capture_output=capture_output, judge_spec=judge_spec,
                            timeout=timeout, command=command, pytest_args=pytest_args)


def _execute_run(path: str, dir: str, *, capture_output: bool, judge_spec: str | None,
                 timeout: float | None, command: str, pytest_args: list[str] | None) -> dict:
    store = Store(Path(dir))
    extra = list(pytest_args or ())
    if any(a.startswith("--nightward") for a in extra):
        raise NightwardError("pytest args may not set --nightward-* options - nightward "
                             "run sets them itself (use --dir for the store)")
    if not Path(path.split("::", 1)[0]).exists():
        # pytest would say so too, but behind an exit code nightward can't tell
        # from a broken install (R1-OPS-08). E.g. an agent deleted the test dir.
        raise NightwardError(f"path {path!r} does not exist (under {Path.cwd()}); "
                             f"nothing was run - {_ABORTED}")
    spec = judge_spec or None
    run_id = uuid.uuid4().hex
    # Build (= validate) the judge before pytest: a typo'd spec or a corrupt
    # ledger must fail in a second, not after the whole suite (R1-LLM-06).
    judge = make_judge(spec, store)
    # One writer per store (D11): the pytest child flushes under this lock (it
    # recognizes the run id), and the recompute below stays inside it too.
    with store_lock(store.root, command, token=run_id):
        return _run_locked(store, path, dir, run_id, spec, judge, extra,
                           capture_output=capture_output, timeout=timeout)


def _run_locked(store: Store, path: str, dir: str, run_id: str, spec: str | None, judge,
                extra: list[str], *, capture_output: bool, timeout: float | None) -> dict:
    try:
        # stdin=DEVNULL: under `nightward mcp` our stdin is the protocol pipe; a
        # child inheriting it hangs on Windows while the server reads it.
        result = subprocess.run(_pytest_cmd(path, dir, run_id, extra), stdin=subprocess.DEVNULL,
                                capture_output=capture_output, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        store.invalidate_report()
        raise NightwardError(
            f"pytest timed out after {timeout}s under {path!r}; {_ABORTED}") from exc
    if result.returncode not in (0, 1):
        store.invalidate_report()
        raise NightwardError(_abort_message(path, result))
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
        "errors": meta.get("errors", 0),
        "deselected": meta.get("deselected", 0),
        "xfailed": meta.get("xfailed", 0),
        # values the built-in timestamp/uuid scrubbers masked: {values, behaviors}
        "scrubbed": meta.get("scrubbed") or {"values": 0, "behaviors": 0},
        # custom scrub.register/register_field rules that matched nothing
        "scrub_unmatched": meta.get("scrub_unmatched") or [],
        "pytest_returncode": result.returncode,
        "output_tail": _output_tail(result),
    }
