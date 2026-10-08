"""Pytest plugin: capture behaviors during a normal test run.

We piggyback on pytest (discovery, fixtures, parametrization, CI) instead of
building a runner. Tests opt in by requesting the `behavior` fixture and calling
it. Behaviors are flushed to .nightward/pending only when --nightward-record is set.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from .core.baseline import Store, digest
from .core.behavior import Behavior, validate_name
from .core.lock import acquire, read_lock, release
from .errors import NightwardError
from .scrub import scrub_counted, unmatched_rules

# Windows tools (Git for Windows without core.longpaths, apps without
# LongPathsEnabled) refuse paths of MAX_PATH (260) characters or more.
_WINDOWS = os.name == "nt"
_MAX_PATH = 259


def _check_path_length(store_root: Path, name: str) -> None:
    # Longest file a behavior gets: <store>/pending.tmp/<name>.received.json
    # (baseline/ and rejected/ are a little shorter).
    longest = len(str(store_root.resolve() / "pending.tmp" / f"{name}.received.json"))
    if _WINDOWS and longest > _MAX_PATH:
        fit = len(name) - (longest - _MAX_PATH)
        raise NightwardError(
            f"behavior name {name!r} is too long for Windows paths here: its store file "
            f"would be {longest} characters (limit {_MAX_PATH}; git can't add it without "
            f"core.longpaths). Use a shorter name (at most {max(fit, 0)} characters at "
            f"this location) or a shorter project path.")


class Recorder:
    def __init__(self, store_root: Path | None = None) -> None:
        # Set when recording: names are checked against the store's path length.
        self.store_root = store_root
        self.behaviors: list[Behavior] = []
        self._seen: dict[str, str] = {}  # casefolded name -> name as captured
        self.masked: dict[str, int] = {}  # name -> values the default scrubbers masked
        # Removal evidence: tests whose every phase passed this run. Only such a
        # test proves that a behavior it no longer captures is really gone.
        self._passed: set[str] = set()
        self._broken: set[str] = set()
        # Deselected (-k/-m) tests: their behaviors were not checked (D21).
        self.deselected: set[str] = set()

    def begin(self, source: str) -> None:
        """A test (re)starts: drop what an earlier attempt of it captured.

        pytest-rerunfailures & co. run the same item again; the last attempt
        wins. Two *different* tests capturing one name stay an error in add().
        """
        stale = [b for b in self.behaviors if b.source == source]
        if not stale:
            return
        self.behaviors = [b for b in self.behaviors if b.source != source]
        for b in stale:
            self._seen.pop(b.name.casefold(), None)
            self.masked.pop(b.name, None)

    def completed(self) -> list[str]:
        return sorted(self._passed - self._broken)

    def pytest_deselected(self, items) -> None:
        self.deselected.update(item.nodeid for item in items)

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "setup":   # a fresh attempt (e.g. a rerun) starts clean
            self._passed.discard(report.nodeid)
            self._broken.discard(report.nodeid)
        if report.failed or report.skipped:   # skips, errors, failures, xfails
            self._broken.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self._passed.add(report.nodeid)

    def add(self, name: str, value, group: str | None = None,
            semantic: bool = False, source: str | None = None,
            scrub: bool = True, path: Path | None = None) -> None:
        validate_name(name)
        if self.store_root is not None:
            _check_path_length(self.store_root, name)
        # Names are filenames: "Total" and "total" are the same file on
        # Windows/macOS, so one would silently overwrite the other.
        prior = self._seen.get(name.casefold())
        if prior == name:
            raise NightwardError(
                f"duplicate behavior name {name!r}: each captured behavior must be unique"
            )
        if prior is not None:
            raise NightwardError(
                f"behavior name {name!r} collides with {prior!r}: names differing only "
                f"in case map to the same file on case-insensitive filesystems"
            )
        self._seen[name.casefold()] = name
        # scrub() -> canonical_json may raise NightwardError on bad payloads;
        # let it surface (naming the behavior) so the offending test fails loudly.
        try:
            # path: the capturing test's file - scopes conftest rules (D20)
            payload, masked = scrub_counted(value, enabled=scrub, path=path)
        except NightwardError as exc:
            raise NightwardError(f"behavior {name!r}: {exc}") from exc
        if masked:
            self.masked[name] = masked
        self.behaviors.append(
            Behavior(name=name, payload=payload, group=group, semantic=semantic,
                     source=source, scrub=scrub)
        )


def pytest_addoption(parser):
    group = parser.getgroup("nightward")
    group.addoption("--nightward-record", action="store_true", default=False,
                    help="Record behaviors to the nightward pending store")
    group.addoption("--nightward-dir", action="store", default=".nightward",
                    help="Nightward storage directory (default: .nightward)")
    group.addoption("--nightward-run-id", action="store", default=None,
                    help="Token recorded in run_meta once the capture is flushed "
                         "(set by `nightward run` to verify the flush happened)")


def pytest_configure(config):
    # xdist splits tests across workers, each with its own Recorder; the
    # controller would flush an empty set and every behavior would read REMOVED.
    if config.getoption("--nightward-record") and getattr(config.option, "numprocesses", None):
        raise pytest.UsageError(
            "--nightward-record cannot run under pytest-xdist; drop -n (or pass -n 0)"
        )
    recording = config.getoption("--nightward-record")
    config._nightward_lock = None
    if recording:
        _lock_for_session(config)
    config._nightward_recorder = Recorder(
        Path(config.getoption("--nightward-dir")) if recording else None)
    config.pluginmanager.register(config._nightward_recorder, "nightward-recorder")


def _lock_for_session(config) -> None:
    """One writer per store (D11), checked before the suite runs (R3-FIN-05).

    Under `nightward run` the runner already holds the lock for this run id.
    A bare `pytest --nightward-record` takes it for the whole session, so a
    busy store is a clean usage error in a second, not a traceback after the
    suite, and no other writer can start between collection and the flush.
    """
    root = Path(config.getoption("--nightward-dir"))
    run_id = config.getoption("--nightward-run-id")
    if run_id and (read_lock(root) or {}).get("token") == run_id:
        return
    try:
        config._nightward_lock = acquire(root, "pytest --nightward-record")
    except NightwardError as exc:
        raise pytest.UsageError(f"nightward: {exc}") from None


def pytest_unconfigure(config):
    held = getattr(config, "_nightward_lock", None)
    if held:
        release(Path(config.getoption("--nightward-dir")), held)
        config._nightward_lock = None


@pytest.fixture
def behavior(request):
    """Capture a named behavior:  behavior("checkout_total", result, group="billing")

    semantic=True opts the behavior into LLM-judge equivalence (v0.2): on a
    fingerprint mismatch the configured judge may rule the change SAME-by-meaning.
    Use it only for nondeterministic free text; deterministic payloads stay exact.

    scrub=False skips all scrubbing for this behavior (built-in timestamp/uuid
    masking and custom rules) - use it when datetimes or uuids ARE the output.
    """
    rec = request.config._nightward_recorder
    rec.begin(request.node.nodeid)

    def capture(name: str, value, *, group: str | None = None,
                semantic: bool = False, scrub: bool = True) -> None:
        rec.add(name, value, group=group, semantic=semantic, source=request.node.nodeid,
                scrub=scrub, path=request.node.path)

    return capture


# Only a session that actually ran its tests produces a capture worth keeping.
# An interrupted / errored / empty session must leave the previous pending set
# alone: flushing its (partial or empty) capture would turn every missing
# behavior into REMOVED, and `approve --all` would then wipe them from the
# baseline.
_COMPLETE = (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED)

# run_meta key -> terminalreporter stats bucket
_COUNTS = (("skipped", "skipped"), ("failed", "failed"), ("errors", "error"),
           ("deselected", "deselected"), ("xfailed", "xfailed"))


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if not config.getoption("--nightward-record") or exitstatus not in _COMPLETE:
        return
    rec = getattr(config, "_nightward_recorder", None)
    if rec is None:
        return
    store = Store(Path(config.getoption("--nightward-dir")))
    # The store lock is already held: by the runner, or by this session since
    # pytest_configure (see _lock_for_session).
    _flush(session, exitstatus, rec, store, config.getoption("--nightward-run-id"))


# Options `nightward run` adds itself; anything else on the command line is
# the user's (passthrough) and makes the run unfit as removal proof (D18).
_OWN_FLAGS = ("--nightward-record",)
_OWN_VALUED = ("--nightward-dir", "--nightward-run-id")
_VERBOSITY = re.compile(r"-[qv]+|--quiet|--verbose")


def _extra_args(config) -> list[str]:
    """Command-line arguments beyond the test paths and nightward's own options."""
    args = [str(a) for a in config.invocation_params.args]
    paths = {str(a) for a in config.args}
    extra, i = [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if a in _OWN_FLAGS or a in paths or _VERBOSITY.fullmatch(a):
            continue
        if a in _OWN_VALUED or (a == "-n" and i < len(args) and args[i] == "0"):
            i += 1     # and its value
            continue
        if a.split("=", 1)[0] in _OWN_VALUED or a in ("-n0", "--numprocesses=0"):
            continue
        extra.append(a)
    return extra


def _scope(session, exitstatus, counts: dict, completed: list[str]) -> dict:
    """How much of the suite this run covered - removal evidence (D18).

    narrowed: -k/-m, deselection (incl. --lf) or a test-id argument.
    clean: a removal can be proven only by a clean whole-suite run: the
    rootdir or the configured testpaths, no extra pytest arguments (nor
    PYTEST_ADDOPTS), exit 0, every collected test passed, nothing skipped,
    xfailed, deselected or errored. clean_doubt says why not.
    """
    config = session.config
    args = [str(a) for a in config.args]
    narrowed = bool(counts["deselected"] or config.option.keyword or config.option.markexpr
                    or any("::" in a for a in args))
    root = Path(config.rootpath).resolve()
    allowed = {root, *((root / t).resolve() for t in config.getini("testpaths"))}
    here = Path(config.invocation_params.dir)
    doubts = []
    partial = [a for a in args if (here / a).resolve() not in allowed
               and (here / a).resolve() not in root.parents]
    if partial or narrowed:
        doubts.append(f"it ran {' '.join(partial) or 'a narrowed selection'}, not the "
                      f"whole suite")
    extra = _extra_args(config)
    if extra:
        doubts.append(f"extra pytest arguments {' '.join(extra)}")
    if os.environ.get("PYTEST_ADDOPTS", "").strip():
        doubts.append("PYTEST_ADDOPTS was set")
    if config.option.collectonly or config.option.setuponly or config.option.setupplan:
        doubts.append("no test ran (--collect-only/--setup-only/--setup-plan)")
    not_run = [f"{counts[k]} {k}" for k in ("failed", "errors", "skipped", "xfailed",
                                             "deselected") if counts.get(k)]
    if not_run:
        doubts.append(", ".join(not_run))
    elif exitstatus != pytest.ExitCode.OK:
        doubts.append(f"pytest exited with {int(exitstatus)}")
    if len(completed) != len(session.items):
        doubts.append(f"only {len(completed)} of {len(session.items)} collected test(s) "
                      f"ran and passed")
    return {"narrowed": narrowed, "clean": not doubts,
            "clean_doubt": "; ".join(dict.fromkeys(doubts)) or None}


def _flush(session, exitstatus, rec: Recorder, store: Store, run_id: str | None) -> None:
    config = session.config
    try:
        store.ensure()
        store.replace_pending(rec.behaviors)
    except BaseException:
        # The old report describes a capture this run meant to replace; leaving
        # it would let `gate` pass on stale data. Fail closed, then surface.
        store.invalidate_report()
        raise

    # Skipped/deselected/xfailed tests don't capture their behavior -> it shows
    # up as a false REMOVED; failed/errored tests make the capture incomplete
    # (the run and the gate fail on it). Record the counts so the runner can
    # act on them, and the tests that completed as per-behavior removal evidence.
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = reporter.stats if reporter else {}
    meta: dict = {key: len(stats.get(stat, [])) for key, stat in _COUNTS}
    meta["completed"] = rec.completed()
    meta["deselected_ids"] = sorted(rec.deselected)
    # Ties run_meta to exactly this flush: `nightward report` trusts pending/
    # only when it still matches (R2-DATA-04).
    meta["pending_digest"] = digest({b.name: b for b in rec.behaviors})
    meta |= _scope(session, exitstatus, meta, meta["completed"])
    last = store.load_run_meta()
    # Which behaviors the default scrubbers touched; "changed" lets `run` show
    # its note when that set moves instead of on every run.
    names = sorted(rec.masked)
    was = (last.get("scrubbed") or {}).get("names")
    meta["scrubbed"] = {"values": sum(rec.masked.values()), "behaviors": len(names),
                        "names": names, "changed": names != was}
    # A custom rule that never fired leaves the user believing noise is handled.
    meta["scrub_unmatched"] = unmatched_rules()
    # Written last: its presence proves to the runner that THIS run's flush landed.
    if run_id:
        meta["run_id"] = run_id
    store.write_run_meta(meta)
