"""Pytest plugin: capture behaviors during a normal test run.

We piggyback on pytest (discovery, fixtures, parametrization, CI) instead of
building a runner. Tests opt in by requesting the `behavior` fixture and calling
it. Behaviors are flushed to .nightward/pending only when --nightward-record is set.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from .core.baseline import Store
from .core.behavior import Behavior, validate_name
from .errors import NightwardError
from .scrub import scrub


class Recorder:
    def __init__(self) -> None:
        self.behaviors: list[Behavior] = []
        self._seen: dict[str, str] = {}  # casefolded name -> name as captured

    def add(self, name: str, value, group: str | None = None,
            semantic: bool = False) -> None:
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
            payload = scrub(value)
        except NightwardError as exc:
            raise NightwardError(f"behavior {name!r}: {exc}") from exc
        self.behaviors.append(
            Behavior(name=name, payload=payload, group=group, semantic=semantic)
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


@pytest.fixture
def behavior(request):
    """Capture a named behavior:  behavior("checkout_total", result, group="billing")

    semantic=True opts the behavior into LLM-judge equivalence (v0.2): on a
    fingerprint mismatch the configured judge may rule the change SAME-by-meaning.
    Use it only for nondeterministic free text; deterministic payloads stay exact.
    """
    rec = request.config._nightward_recorder

    def capture(name: str, value, *, group: str | None = None,
                semantic: bool = False) -> None:
        rec.add(name, value, group=group, semantic=semantic)

    return capture


# Only a session that actually ran its tests produces a capture worth keeping.
# An interrupted / errored / empty session must leave the previous pending set
# alone: flushing its (partial or empty) capture would turn every missing
# behavior into REMOVED, and `approve --all` would then wipe them from the
# baseline.
_COMPLETE = (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED)


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    if not config.getoption("--nightward-record") or exitstatus not in _COMPLETE:
        return
    rec = getattr(config, "_nightward_recorder", None)
    if rec is None:
        return
    store = Store(Path(config.getoption("--nightward-dir")))
    try:
        store.ensure()
        store.replace_pending(rec.behaviors)
    except BaseException:
        # The old report describes a capture this run meant to replace; leaving
        # it would let `gate` pass on stale data. Fail closed, then surface.
        store.invalidate_report()
        raise

    # Skipped tests don't capture their behavior -> it shows up as a false
    # REMOVED; failed/errored tests make the capture incomplete (the run and
    # the gate fail on it). Record the counts so the runner can act on them.
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = reporter.stats if reporter else {}
    meta = {key: len(stats.get(stat, []))
            for key, stat in (("skipped", "skipped"), ("failed", "failed"), ("errors", "error"))}
    # Written last: its presence proves to the runner that THIS run's flush landed.
    run_id = config.getoption("--nightward-run-id")
    if run_id:
        meta["run_id"] = run_id
    store.write_run_meta(meta)
