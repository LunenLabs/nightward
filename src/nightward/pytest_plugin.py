"""Pytest plugin: capture behaviors during a normal test run.

We piggyback on pytest (discovery, fixtures, parametrization, CI) instead of
building a runner. Tests opt in by requesting the `behavior` fixture and calling
it. Behaviors are flushed to .nightward/pending only when --nightward-record is set.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import pytest

from . import scrub as _scrub
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
        # name -> {custom rule text: values it replaced in that behavior} (D28)
        self.rule_hits: dict[str, dict[str, int]] = {}
        # Tests whose every phase passed this run: tells the human whether a
        # REMOVED behavior's test ran at all (a diagnostic, never proof - D29).
        self._passed: set[str] = set()
        self._broken: set[str] = set()
        # Deselected (-k/-m) tests: their behaviors were not checked (D21).
        self.deselected: set[str] = set()
        # Test files tests were collected from, and items a hook dropped without a
        # pytest_deselected report: what the run left out, for diagnostics.
        self.collected_files: set[str] = set()
        self.filtered = 0

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
            self.rule_hits.pop(b.name, None)

    def completed(self) -> list[str]:
        return sorted(self._passed - self._broken)

    def pytest_deselected(self, items) -> None:
        self.deselected.update(item.nodeid for item in items)

    @pytest.hookimpl(wrapper=True)
    def pytest_collection_modifyitems(self, session, config, items):
        # Files that yielded tests (pytest builds a collector for every file in
        # a directory it visits, even when one file was asked for).
        self.collected_files.update(_key(Path(i.path)) for i in items)
        before, reported = len(items), len(self.deselected)
        result = yield
        # Items gone without a pytest_deselected report: filtered by a hook.
        self.filtered += before - len(items) - (len(self.deselected) - reported)
        return result

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
            before = _scrub.hits()
            payload, masked = scrub_counted(value, enabled=scrub, path=path)
            replaced = _scrub.hits() - before
            if replaced:
                self.rule_hits[name] = dict(replaced)
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
    # Which files may register scrub rules (D28): pytest's own notion of a test module.
    _scrub.set_test_files(config.getini("python_files"))
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
    _flush(session, rec, store, config.getoption("--nightward-run-id"))


# Directories pytest never recurses into by default (its own norecursedirs
# default, plus virtualenvs): never the project's suite. A project's own
# norecursedirs is NOT honored - it is one more way to leave tests out.
_NEVER_SUITE = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv",
                "{arch}", "__pycache__")
_DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")


def _key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _matches(path: Path, pattern: str) -> bool:
    # pytest's fnmatch_ex: a pattern without a separator matches the file name
    if "/" not in pattern and os.sep not in pattern:
        return fnmatch.fnmatch(path.name, pattern)
    return fnmatch.fnmatch(path.as_posix(), "*/" + pattern.replace(os.sep, "/").lstrip("/"))


def _test_files_on_disk(root: Path, patterns: list[str], skip: Path) -> list[Path]:
    """Every file under `root` that looks like a test module, wherever pytest's
    configuration (norecursedirs, testpaths, collect_ignore, --ignore, hooks,
    plugins) would or wouldn't look."""
    found = []
    for here, dirs, files in os.walk(root):
        base = Path(here)
        dirs[:] = [d for d in dirs
                   if not any(fnmatch.fnmatch(d, pat) for pat in _NEVER_SUITE)
                   and not (base / d / "pyvenv.cfg").exists()
                   and not (base / d / "conda-meta").is_dir()
                   and _key(base / d) != _key(skip)]
        found += [base / f for f in files
                  if any(_matches(base / f, pat) for pat in patterns)]
    return found


def _uncollected(config, rec: Recorder) -> list[str]:
    """Test files on disk that this run did not collect - a diagnostic for
    REMOVED behaviors: a capture may have moved into one of them, whatever
    left it out. It never authorizes a removal (D29): in-file exclusion
    (__test__, pycollect hooks) can't be seen this way at all.
    """
    root = Path(config.rootpath).resolve()
    patterns = list(dict.fromkeys([*config.getini("python_files"), *_DEFAULT_PYTHON_FILES]))
    skip = Path(config.invocation_params.dir) / config.getoption("--nightward-dir")
    return sorted(p.relative_to(root).as_posix()
                  for p in _test_files_on_disk(root, patterns, skip)
                  if _key(p) not in rec.collected_files)


def _scope(session, counts: dict, rec: Recorder) -> dict:
    """How much of the suite this run covered.

    narrowed: -k/-m, deselection (incl. --lf) or a test-id argument (D21/D23).
    uncollected / filtered: test files on disk the run left out, and items a
    hook dropped without reporting them - shown to the human who decides on a
    REMOVED behavior, never proof of anything (D29).
    """
    config = session.config
    args = [str(a) for a in config.args]
    narrowed = bool(counts["deselected"] or config.option.keyword or config.option.markexpr
                    or any("::" in a for a in args))
    return {"narrowed": narrowed, "uncollected": _uncollected(config, rec),
            "filtered": max(rec.filtered, 0)}


def _rule_report(rec: Recorder, previous) -> list[dict]:
    """Per custom rule: values and behaviors it replaced this run, whether that
    count changed since the last run, and whether its replacement is a visible
    <PLACEHOLDER> or a plausible value (a rewrite) - D28, R4-LLM-02."""
    was = {r.get("rule"): r.get("values") for r in previous or () if isinstance(r, dict)}
    out = []
    for text, placeholder in _scrub.rules():
        behaviors = sorted(n for n, hit in rec.rule_hits.items() if hit.get(text))
        values = sum(rec.rule_hits[n][text] for n in behaviors)
        out.append({"rule": text, "values": values, "behaviors": behaviors,
                    "was": was.get(text), "changed": was.get(text) != values,
                    "placeholder": placeholder})
    return out


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
    # act on them, and which tests completed (shown for REMOVED behaviors).
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = reporter.stats if reporter else {}
    meta: dict = {key: len(stats.get(stat, [])) for key, stat in _COUNTS}
    meta["completed"] = rec.completed()
    meta["deselected_ids"] = sorted(rec.deselected)
    # Ties run_meta to exactly this flush: `nightward report` trusts pending/
    # only when it still matches (R2-DATA-04).
    meta["pending_digest"] = digest({b.name: b for b in rec.behaviors})
    meta |= _scope(session, meta, rec)
    last = store.load_run_meta()
    # Which behaviors the default scrubbers touched; "changed" lets `run` show
    # its note when that set moves instead of on every run.
    names = sorted(rec.masked)
    was = (last.get("scrubbed") or {}).get("names")
    meta["scrubbed"] = {"values": sum(rec.masked.values()), "behaviors": len(names),
                        "names": names, "changed": names != was}
    # A custom rule that never fired leaves the user believing noise is handled.
    meta["scrub_unmatched"] = unmatched_rules()
    meta["scrub_rules"] = _rule_report(rec, last.get("scrub_rules"))
    # Written last: its presence proves to the runner that THIS run's flush landed.
    if run_id:
        meta["run_id"] = run_id
    store.write_run_meta(meta)
