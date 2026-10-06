"""Pytest plugin: capture behaviors during a normal test run.

We piggyback on pytest (discovery, fixtures, parametrization, CI) instead of
building a runner. Tests opt in by requesting the `behavior` fixture and calling
it. Behaviors are flushed to .nightward/pending only when --nightward-record is set.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from .core.baseline import Store
from .core.behavior import Behavior, validate_name
from .core.lock import read_lock, store_lock
from .errors import NightwardError
from .scrub import scrub_counted, unmatched_rules


class Recorder:
    def __init__(self) -> None:
        self.behaviors: list[Behavior] = []
        self._seen: dict[str, str] = {}  # casefolded name -> name as captured
        self.masked: dict[str, int] = {}  # name -> values the default scrubbers masked
        # Removal evidence: tests whose every phase passed this run. Only such a
        # test proves that a behavior it no longer captures is really gone.
        self._passed: set[str] = set()
        self._broken: set[str] = set()

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
            scrub: bool = True) -> None:
        validate_name(name)
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
            payload, masked = scrub_counted(value, enabled=scrub)
        except NightwardError as exc:
            raise NightwardError(f"behavior {name!r}: {exc}") from exc
        if masked:
            self.masked[name] = masked
        self.behaviors.append(
            Behavior(name=name, payload=payload, group=group, semantic=semantic,
                     source=source)
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
    config._nightward_recorder = Recorder()
    config.pluginmanager.register(config._nightward_recorder, "nightward-recorder")


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
                scrub=scrub)

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
    run_id = config.getoption("--nightward-run-id")
    # One writer per store (D11). Under `nightward run` the runner already
    # holds the lock for this run id; a bare `pytest --nightward-record` takes
    # it for the flush, and fails fast if another writer is busy.
    owned = run_id and (read_lock(store.root) or {}).get("token") == run_id
    with contextlib.nullcontext() if owned else store_lock(store.root,
                                                           "pytest --nightward-record"):
        _flush(session, rec, store, run_id)


def _scope(session, deselected: int) -> dict:
    """How much of the suite this run covered - removal evidence (D13).

    narrowed: -k/-m, deselection (incl. --lf) or a test-id argument; such a run
    never proves a removal. whole_suite: not narrowed, and every path argument
    is the rootdir (or above it) or a configured testpath.
    """
    config = session.config
    args = [str(a) for a in config.args]
    narrowed = bool(deselected or config.option.keyword or config.option.markexpr
                    or any("::" in a for a in args))
    root = Path(config.rootpath).resolve()
    allowed = {root, *((root / t).resolve() for t in config.getini("testpaths"))}
    here = Path(config.invocation_params.dir)
    targets = [(here / a).resolve() for a in args]
    whole = not narrowed and all(t in allowed or t in root.parents for t in targets)
    return {"narrowed": narrowed, "whole_suite": whole,
            "collected_files": sorted({item.nodeid.split("::", 1)[0]
                                       for item in session.items})}


def _flush(session, rec: Recorder, store: Store, run_id: str | None) -> None:
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
    meta |= _scope(session, meta["deselected"])
    # The test that LAST captured each behavior, carried across runs: a capture
    # moved to another test must not leave its old test as removal proof.
    previous = store.load_run_meta().get("sources")
    meta["sources"] = {**(previous if isinstance(previous, dict) else {}),
                       **{b.name: b.source for b in rec.behaviors if b.source}}
    meta["scrubbed"] = {"values": sum(rec.masked.values()), "behaviors": len(rec.masked)}
    # A custom rule that never fired leaves the user believing noise is handled.
    meta["scrub_unmatched"] = unmatched_rules()
    # Written last: its presence proves to the runner that THIS run's flush landed.
    if run_id:
        meta["run_id"] = run_id
    store.write_run_meta(meta)
