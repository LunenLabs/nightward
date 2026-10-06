"""nightward CLI - init / run / review / doctor / approve / reject / gate / status."""
from __future__ import annotations

import errno
import functools
import json
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from .core.baseline import Store, digest
from .core.diff import REMOVED, UNCHANGED, compare
from .core.lock import store_lock
from .errors import NightwardError
from .runner import execute_run, is_stale, judge_from_meta, recompute, recompute_capture
from .signal import status_payload
from .view import build_site

# Captured payloads and diffs can contain any character (e.g. Hangul). On a
# non-UTF-8 Windows console (cp949) rich's legacy win32 writer raises
# UnicodeEncodeError. Route output through sys.stdout as UTF-8 (replacing only
# what truly can't be shown) and disable the legacy writer so nothing crashes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


class _Stdout:
    """sys.stdout that outlives its reader (`nightward review | head`).

    A reader that quits makes writes fail with BrokenPipeError (OSError EINVAL
    on Windows). Point the fd at devnull and finish silently, keeping the
    command's own exit code: `gate` must stay 1 on a breach even when nobody
    reads its verdict, and `review | head` must not exit 120 under pipefail.
    """

    reader_gone = False

    def write(self, text: str) -> int:
        if not self.reader_gone:
            try:
                sys.stdout.write(text)
            except OSError as exc:
                self._gone(exc)
        return len(text)

    def flush(self) -> None:
        if not self.reader_gone:
            try:
                sys.stdout.flush()
            except OSError as exc:
                self._gone(exc)

    def _gone(self, exc: OSError) -> None:
        if exc.errno not in (errno.EPIPE, errno.EINVAL):
            raise exc
        self.reader_gone = True
        # The interpreter flushes sys.stdout again at exit; give that a sink.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)

    def __getattr__(self, name: str):
        return getattr(sys.stdout, name)


_stdout = _Stdout()

app = typer.Typer(
    help="nightward - regression firewall for AI-driven changes",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(file=_stdout, legacy_windows=False)
err_console = Console(stderr=True, legacy_windows=False)

DEFAULT_DIR = ".nightward"
DEFAULT_SITE = "nightward-site"   # `view` output: holds captured data, never commit it

# Store entries that are per-run state, relative to the store dir.
# judge_verdicts.json and rejected/ are deliberately NOT here: the verdict
# ledger keeps judged-SAME boundaries deterministic on fresh clones / CI, and a
# rejection must protect every clone, not just the machine that made it (D17).
TRANSIENT_ENTRIES = ("pending/", "report.json", "run_meta.json",
                     "pending.tmp/", "**/*.tmp", ".lock", "reviewed.json")
# Ignore rules older versions of `init` wrote that must now go.
LEGACY_ENTRIES = ("rejected/",)
GITIGNORE_HEADER = "# nightward: approved baseline IS committed; transient state is not"

# Everything rich prints is parsed as markup, so captured data (names, groups,
# diffs, payload keys) must go through escape() - a name like "total[eur]" would
# otherwise vanish from the output and a payload containing "[/x]" would crash it.


def handle_errors(fn):
    """Turn NightwardError into a clean stderr message + exit code 2."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NightwardError as exc:
            err_console.print(f"[red]error:[/red] {escape(str(exc))}")
            raise typer.Exit(2) from None
    return wrapper


def _store(dir_: str) -> Store:
    return Store(Path(dir_))


def _check_dir(dir_: str) -> None:
    p = Path(dir_)
    if p.exists() and not p.is_dir():
        raise NightwardError(f"--dir {dir_!r} exists but is not a directory")


def _store_above(dir_: str) -> str | None:
    """A store named `dir_` in a parent directory (cwd is a subdirectory of the project)."""
    if Path(dir_).is_absolute():
        return None
    for parent in Path.cwd().parents:
        if (parent / dir_).is_dir():
            return os.path.relpath(parent / dir_)
    return None


def _missing_store_message(dir_: str) -> str:
    above = _store_above(dir_)
    if above:
        return (f"no nightward store at {dir_!r}, but found {above!r} - run from the "
                f"project root, or pass --dir {above}")
    return (f"no nightward store at {dir_!r} (under {Path.cwd()}) - check --dir, or "
            f"create one with `nightward init` and `nightward run`")


def _existing_store(dir_: str) -> Store:
    """The store for a command that reads it: a typo'd --dir or a run from a
    subdirectory must not read as an empty, "intact" store (R1-OPS-07)."""
    _check_dir(dir_)
    if not Path(dir_).is_dir():
        raise NightwardError(_missing_store_message(dir_))
    return _store(dir_)


def _require_report(store: Store) -> dict:
    report = store.load_report()
    if report is None:
        raise NightwardError("no report - run `nightward run` (a run that aborted or "
                             "timed out invalidates the last report)")
    return report


def _incomplete_text(incomplete: dict) -> str:
    return (f"{incomplete.get('failed', 0)} failed, {incomplete.get('errors', 0)} error(s) "
            f"in the capture run - fix them and re-run `nightward run`")


def _incomplete_short(incomplete: dict) -> str:
    parts = [f"{incomplete[k]} {word} capture test(s)"
             for k, word in (("failed", "failed"), ("errors", "errored")) if incomplete.get(k)]
    return ", ".join(parts)


STALE_MESSAGE = ("[red]report is stale[/red] - the baseline or the capture changed since "
                 "the last report; re-run `nightward run` (or `nightward report` after "
                 "`pytest --nightward-record`)")


def _mark_reviewed(store: Store, report: dict | None, via: str) -> None:
    """Record the capture a human just saw, so approve promotes exactly that (D10).

    Only human surfaces call this (run/review/view) - never MCP: an agent's
    run between review and approve must not choose what the approval covers.
    """
    if report and report.get("pending_digest"):
        store.mark_reviewed(report["pending_digest"], via)


def _check_reviewed(store: Store, pending) -> None:
    mark = store.load_reviewed()
    if not mark:
        raise NightwardError("no human has reviewed this capture yet - run `nightward "
                             "review` (or `nightward run` / `nightward view`), then approve")
    if mark.get("pending_digest") != digest(pending):
        raise NightwardError(
            f"the capture changed since you last reviewed it (with `nightward "
            f"{mark.get('via', 'review')}`) - another run (an agent's nightward_run, CI, a "
            f"teammate) captured again. Run `nightward review` again, then approve what "
            f"it shows.")


def _store_prefix(dir_: str) -> str | None:
    """The store as a path a .gitignore here can name, or None when it lives
    outside the current directory."""
    p = Path(dir_)
    if p.is_absolute():
        try:
            p = p.relative_to(Path.cwd())
        except ValueError:
            return None
    prefix = p.as_posix()
    if prefix == ".." or prefix.startswith("../"):
        return None
    return prefix


def _gitignore_lines(dir_: str) -> list[str] | None:
    """Ignore rules for the store's transient entries, or None when the store
    lives outside the current directory (a .gitignore here can't name it)."""
    prefix = _store_prefix(dir_)
    if prefix is None:
        return None
    return [GITIGNORE_HEADER, f"{DEFAULT_SITE}/",
            *(f"{prefix}/{entry}" for entry in TRANSIENT_ENTRIES)]


def _legacy_gitignore_lines(dir_: str) -> list[str]:
    prefix = _store_prefix(dir_)
    return [] if prefix is None else [f"{prefix}/{entry}" for entry in LEGACY_ENTRIES]


def _git_ignored(path: Path) -> bool | None:
    """Whether git ignores `path` (None: no git, or not inside a repository)."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", str(path)], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(r.returncode)


def _warn_unless_ignored(path: Path, what: str) -> None:
    # Only a nudge: a rule in a parent .gitignore or info/exclude counts too.
    if _git_ignored(path) is False:
        err_console.print(f"[yellow]warning:[/yellow] {escape(str(path))} is not git-ignored "
                          f"and {what} - run `nightward init` to add the .gitignore rules",
                          soft_wrap=True)


def _print_summary(report: dict) -> None:
    c = report["counts"]
    # Same word as status --json and the dashboard: a run with failing capture
    # tests is never "intact", even when nothing captured moved (R2-DATA-03).
    incomplete = report.get("incomplete")
    gap = (_incomplete_short(incomplete) if incomplete else "")
    if report["boundary"] == "intact" and incomplete:
        console.print(f"\n[bold]Boundary:[/bold] [red]incomplete[/red] ({gap})")
    elif report["boundary"] == "intact":
        console.print("\n[bold]Boundary:[/bold] [green]intact[/green]")
    else:
        console.print(f"\n[bold]Boundary:[/bold] [red]breached[/red] "
                      f"({report['unapproved']} unapproved{'; ' + gap if gap else ''})")
    console.print(f"unchanged={c['unchanged']} changed={c['changed']} "
                  f"new={c['new']} removed={c['removed']}")
    if c.get("judged_same"):
        console.print(f"[dim]{c['judged_same']} fingerprint mismatch(es) ruled "
                      f"semantically SAME by the judge[/dim]")
    judge = report.get("judge") or {}
    if judge.get("unavailable"):
        err_console.print(
            f"[yellow]warning:[/yellow] judge {escape(judge['spec'])} unavailable "
            f"({escape(judge['unavailable'])}); {len(judge['compared_exactly'])} semantic "
            f"behavior(s) compared exactly", soft_wrap=True)
    for group, items in report.get("blast_radius", {}).items():
        console.print(f"\n[yellow]group: {escape(group)}[/yellow]")
        for it in items:
            judged = (f" [dim](judged DIFFERENT by {escape(it['judge_model'])})[/dim]"
                      if it.get("judged") else "")
            console.print(f"  - [[cyan]{it['kind']}[/cyan]] {escape(it['name'])}{judged}")


@app.command()
@handle_errors
def init(dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir")):
    """Create the nightward store and add ignore rules to .gitignore."""
    _check_dir(dir)
    store = _store(dir)
    store.ensure()
    console.print(f"[green]created[/green] {escape(str(store.root))}/ (baseline, pending)")

    lines = _gitignore_lines(dir)
    if lines is None:
        console.print(f"[yellow]note:[/yellow] {escape(dir)} is outside this directory - "
                      "add ignore rules for its transient entries "
                      f"({', '.join(TRANSIENT_ENTRIES)}) to the right .gitignore yourself")
        lines = []
    gi = Path(".gitignore")
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    legacy = [ln for ln in _legacy_gitignore_lines(dir) if ln in existing]
    if legacy:
        existing = [ln for ln in existing if ln not in legacy]
        eol = "\r\n" if b"\r\n" in gi.read_bytes() else "\n"   # keep the file's style
        gi.write_text(eol.join(existing) + eol, encoding="utf-8", newline="")
        console.print(f"[green]updated[/green] .gitignore: removed {escape(', '.join(legacy))} "
                      f"- rejections (rejected/) are committed now, so they protect every "
                      f"clone", soft_wrap=True)
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        with gi.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")
        console.print(f"[green]updated[/green] .gitignore (+{len(missing)} lines)")
    console.print("\nNext: capture behaviors with the `behavior` pytest fixture, "
                  "then `nightward run <path>` and `nightward approve --all`.")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@handle_errors
def run(ctx: typer.Context,
        path: str = typer.Argument(".", help="Path passed to pytest"),
        dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir"),
        judge: str | None = typer.Option(
            None, help="Semantic judge for semantic=True behaviors, as provider:model "
                       "(e.g. anthropic:claude-haiku-4-5, persona:editor). "
                       "Default: $NIGHTWARD_JUDGE")):
    """Re-run tests, capture behaviors, compute the blast radius.

    Extra pytest arguments go after `--`: nightward run tests -- -m "not gpu" -p no:randomly
    (a -k/-m/deselecting run never proves a removal).
    """
    _check_dir(dir)
    if dir == DEFAULT_DIR and not Path(dir).exists() and _store_above(dir):
        # pytest finds the rootdir from anywhere; a second store here would
        # report every behavior as NEW (R1-OPS-07).
        raise NightwardError(_missing_store_message(dir))
    console.print(f"[dim]$ pytest {escape(path)} --nightward-record "
                  f"--nightward-dir {escape(dir)}[/dim]")
    extra = [a for a in ctx.args if a != "--"]
    result = execute_run(path, dir, judge_spec=judge, pytest_args=extra)
    not_run = [f"{result[k]} {k}" for k in ("skipped", "deselected", "xfailed") if result[k]]
    if not_run:
        err_console.print(f"[yellow]warning:[/yellow] {', '.join(not_run)} test(s) - "
                          "behaviors they capture appear as REMOVED; blast radius may show "
                          "false positives")
    scrubbed = result["scrubbed"]
    if scrubbed["values"]:
        # Masking is noise control, but it can also hide a real datetime change.
        err_console.print(f"[dim]note: default scrubbers masked {scrubbed['values']} value(s) "
                          f"in {scrubbed['behaviors']} behavior(s) (timestamps/uuids) - opt "
                          f"out with scrub=False[/dim]", soft_wrap=True)
    for rule in result["scrub_unmatched"]:
        # The user believes this noise is handled; it isn't (R1-WEB-03).
        why = ("no captured dict has that key" if rule.startswith("register_field(") else
               "patterns see the canonical JSON text, where a '\"' inside a string is "
               "written '\\\"' and a newline '\\n' - see `help(nightward.scrub.register)`")
        err_console.print(f"[yellow]warning:[/yellow] scrub rule {escape(rule)} matched "
                          f"nothing in this run ({escape(why)})", soft_wrap=True)
    _print_summary(result["report"])
    _mark_reviewed(_store(dir), result["report"], "run")
    # A committed report.json lets a CI `gate` without `run` pass on an old verdict.
    _warn_unless_ignored(Path(dir) / "report.json",
                         "per-run state (pending/, report.json, run_meta.json) must not be "
                         "committed")
    _exit_if_incomplete(result["report"], result["pytest_returncode"])


def _exit_if_incomplete(report: dict, pytest_returncode: int = 0) -> None:
    incomplete = report.get("incomplete")
    if incomplete or pytest_returncode == 1:
        # A failing capture test means behaviors are missing from the blast
        # radius; a green exit here would let CI merge it (`gate` fails too).
        detail = (_incomplete_text(incomplete) if incomplete
                  else "pytest reported failures - fix them and re-run `nightward run`")
        err_console.print(f"\n[red]capture incomplete:[/red] {detail}")
        raise typer.Exit(1)


@app.command("report")
@handle_errors
def report_cmd(dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir")):
    """Compute the verdict from the capture `pytest --nightward-record` already wrote,
    without running pytest again (for CI jobs that run pytest themselves)."""
    store = _existing_store(dir)
    report = recompute_capture(store)
    _print_summary(report)
    _mark_reviewed(store, report, "report")
    _exit_if_incomplete(report)


NAMES_ARG = typer.Argument(None, help="Only these behaviors (default: all)",
                           show_default=False)
GROUP_OPT = typer.Option(None, "--group", help="Only behaviors in this group (repeatable)",
                         show_default=False)


def _scope(names: list[str] | None, groups: list[str] | None, candidates: dict[str, str],
           what: str) -> set[str]:
    """Names (of `candidates`: name -> group) selected by NAME args and --group.

    A NAME that isn't a candidate is an error, not an empty result: a typo
    must not read as "nothing changed".
    """
    if names:
        unknown = [n for n in names if n not in candidates]
        if unknown:
            raise NightwardError(f"not {what}: {', '.join(unknown)}")
    return {n for n, g in candidates.items()
            if (not names or n in names) and (not groups or g in groups)}


def _print_diff(it: dict, max_lines: int) -> None:
    diff = it.get("diff", "")
    if not diff:
        console.print("[dim](no text diff)[/dim]")
        return
    lines = diff.splitlines()
    if max_lines <= 0 or len(lines) <= max_lines:
        console.print(escape(diff))
        return
    console.print(escape("\n".join(lines[:max_lines])))
    console.print(f"[dim]... {len(lines) - max_lines:,} more diff line(s) - see all with "
                  f"`nightward review {escape(it['name'])} --max-lines 0`[/dim]",
                  soft_wrap=True)


@app.command()
@handle_errors
def review(names: list[str] | None = NAMES_ARG,
           group: list[str] | None = GROUP_OPT,
           max_lines: int = typer.Option(60, "--max-lines",
                                         help="Diff lines shown per behavior; 0 = all"),
           dir: str = typer.Option(DEFAULT_DIR)):
    """Show the blast radius with diffs, plus what the judge ruled SAME."""
    store = _existing_store(dir)
    report = _require_report(store)
    if is_stale(store, report):
        # Its diffs compare inputs that are no longer on disk - don't show them.
        console.print(STALE_MESSAGE)
        raise typer.Exit(1)
    if report.get("incomplete"):
        err_console.print(f"[yellow]warning:[/yellow] capture incomplete: "
                          f"{_incomplete_text(report['incomplete'])}")
    blast = report.get("blast_radius", {})
    judged_same = report.get("judged_same") or []
    candidates = {it["name"]: g for g, items in blast.items() for it in items}
    candidates |= {it["name"]: it.get("group") or "(ungrouped)" for it in judged_same}
    wanted = _scope(names, group, candidates,
                    "in the last report's blast radius (unchanged behaviors have no diff)")
    blast = {g: kept for g, items in blast.items()
             if (kept := [it for it in items if it["name"] in wanted])}
    judged_same = [it for it in judged_same if it["name"] in wanted]
    intact = report.get("boundary") == "intact"
    _mark_reviewed(store, report, "review")
    if not blast:
        if not judged_same:
            console.print("[green]boundary intact - nothing to review[/green]" if intact
                          else "[green]nothing to review in that selection[/green]")
            return
        console.print("[green]boundary intact[/green] - no unapproved change" if intact
                      else "no unapproved change in that selection")
    for g, items in blast.items():
        console.print(f"\n[yellow]group: {escape(g)}[/yellow]")
        for it in items:
            console.print(f"\n[bold][[cyan]{it['kind']}[/cyan]] {escape(it['name'])}[/bold]")
            if it.get("judged"):
                console.print(f"[dim]judged DIFFERENT by {escape(it['judge_model'])}: "
                              f"{escape(it.get('judge_reason', ''))}[/dim]")
            _print_diff(it, max_lines)
    if judged_same:
        # Outside the boundary, but a wrong SAME is a hole in the gate: show the
        # exact wording the judge accepted so a human can audit it (R1-LLM-04).
        console.print(f"\n[yellow]ruled semantically SAME by the judge[/yellow] "
                      f"({len(judged_same)}) - not in the boundary; audit the wording:")
        for it in judged_same:
            console.print(f"\n[bold][[cyan]SAME[/cyan]] {escape(it['name'])}[/bold] "
                          f"[dim]{escape(it.get('judge_model', ''))}: "
                          f"{escape(it.get('judge_reason', ''))}[/dim]")
            _print_diff(it, max_lines)


def _standing_rejections(store: Store, baseline, pending) -> set[str]:
    """Names whose current state is exactly what the user rejected.

    The record is the received behavior, or the approved one for a rejected
    removal; a later, different payload is a new change and is not held.
    """
    held = set()
    for name, rec in store.load_rejected().items():
        current = pending.get(name) or baseline.get(name)
        if current is not None and (current.fingerprint(), current.group) == (
                rec.fingerprint(), rec.group):
            held.add(name)
    return held


# Outcomes that leave a test's behaviors uncaptured (a false REMOVED).
_NOT_RUN = ("skipped", "failed", "errors", "deselected", "xfailed")


def _sources(name: str, b, meta: dict) -> set[str]:
    """Every test known to capture `name`: the approved record and the latest run's."""
    latest = (meta.get("sources") or {}).get(name)
    return {s for s in (b.source, latest) if isinstance(s, str)}


def _run_doubt(baseline, meta: dict) -> str | None:
    """Why the last run can't prove any removal, or None (D13 (a))."""
    if meta.get("narrowed", True):
        return ("the last run was narrowed (-k/-m, deselected tests or a test id) - "
                "only a whole-suite run proves removals")
    collected = set(meta.get("collected_files") or ())
    files = {s.split("::", 1)[0] for n, b in baseline.items() for s in _sources(n, b, meta)}
    missing = sorted(files - collected)
    if missing:
        shown = ", ".join(missing[:3]) + (" ..." if len(missing) > 3 else "")
        return (f"the last run did not collect {shown} - only a whole-suite run "
                f"proves removals")
    return None


def _removal_doubt(name: str, b, meta: dict) -> str | None:
    """Why a REMOVED behavior may be false, or None when the run proves it gone.

    Proof (D13): every test known to capture it ran to completion this run and
    did not capture it. Without any recorded test (legacy baselines), only a
    clean whole-suite run proves it.
    """
    sources = _sources(name, b, meta)
    if sources:
        not_done = sorted(sources - set(meta.get("completed", ())))
        if not not_done:
            return None
        return (f"its test {not_done[0]} did not run to completion this run (skipped, "
                f"failed, deselected, xfailed, deleted, or outside the run path)")
    counts = [f"{meta[k]} {k}" for k in _NOT_RUN if meta.get(k)]
    if not meta.get("whole_suite") or counts:
        detail = f" ({', '.join(counts)})" if counts else ""
        return (f"no recorded source test - only a clean whole-suite run proves its "
                f"removal{detail}")
    return None


def _backfill_sources(store: Store, baseline, pending) -> int:
    """Point unchanged baselines at the test that captures them now (D13)."""
    n = 0
    for name, b in baseline.items():
        p = pending.get(name)
        if (p is not None and p.source and p.source != b.source
                and p.fingerprint() == b.fingerprint() and p.group == b.group):
            store.refresh_source(name, p.source)
            n += 1
    return n


def _approve_one(store: Store, name: str, baseline, pending) -> str:
    if name in pending:
        store.approve(name)
        return "approved"
    if name in baseline:
        store.approve_removal(name)
        return "removed"
    raise NightwardError(f"nothing to approve for {name!r}")


@app.command()
@handle_errors
def approve(name: str | None = typer.Argument(None),
            all_: bool = typer.Option(False, "--all", help="Approve every NEW/CHANGED behavior"),
            include_removed: bool = typer.Option(
                False, "--include-removed",
                help="With --all, also accept REMOVED behaviors that a whole-suite run "
                     "proves gone: their test ran to completion without capturing them "
                     "(drops them from the baseline)"),
            dir: str = typer.Option(DEFAULT_DIR)):
    """Promote pending behavior(s) into the approved baseline."""
    if all_ and name:
        raise NightwardError("give a behavior name or --all, not both")
    store = _existing_store(dir)
    with store_lock(store.root, "nightward approve"):
        _approve(store, dir, name, all_, include_removed)


def _approve(store: Store, dir: str, name: str | None, all_: bool,
             include_removed: bool) -> None:
    baseline = store.load_baseline()
    pending = store.load_pending()
    if not baseline and not pending:
        raise NightwardError(f"nothing captured in {dir!r} yet - run `nightward run` first")
    # Approve what the human saw, not what an agent captured after it (D10).
    _check_reviewed(store, pending)
    # Reuse the last run's judge (cached verdicts): --all then approves exactly
    # what the report lists as unapproved, and judged-SAME behaviors don't flip
    # back to CHANGED the moment something else is approved. Approving a
    # judged-SAME rewording explicitly by name still re-anchors it.
    judge = judge_from_meta(store)

    held: list[str] = []
    doubts: dict[str, str] = {}
    kept_rejected: list[str] = []
    if all_:
        changes = [c for c in compare(baseline, pending, judge=judge, with_diff=False)
                   if c.kind != UNCHANGED]
        removed = [c.name for c in changes if c.kind == REMOVED]
        if include_removed:
            # A test that didn't run captures nothing and looks REMOVED; approving
            # that would silently shrink the boundary. Only proven removals go.
            meta = store.load_run_meta()
            run_doubt = _run_doubt(baseline, meta)
            doubts = {n: why for n in removed
                      if (why := run_doubt or _removal_doubt(n, baseline[n], meta))}
            held = list(doubts)
        else:
            held = removed
        # A confirmed regression must never ride along with a bulk approval.
        rejected = _standing_rejections(store, baseline, pending)
        kept_rejected = [c.name for c in changes if c.name in rejected and c.name not in held]
        targets = [c.name for c in changes if c.name not in held and c.name not in rejected]
    elif name:
        targets = [name]
    else:
        raise NightwardError("specify a behavior name or --all")
    if all_:
        refreshed = _backfill_sources(store, baseline, pending)
        if refreshed:
            console.print(f"[dim]refreshed the recorded test of {refreshed} unchanged "
                          f"behavior(s) (removal evidence only)[/dim]")
    if not targets and not held and not kept_rejected:
        console.print("nothing to approve - boundary already intact")
        return

    for n in targets:
        verb = _approve_one(store, n, baseline, pending)
        # The short fingerprint ties the approval to the reviewed content.
        what = f" ({pending[n].fingerprint()[:8]})" if n in pending else ""
        console.print(f"[green]{verb}[/green] {escape(n)}{what}")
        if name and store.clear_rejection(n):
            console.print(f"  [dim]cleared the earlier rejection of {escape(n)}[/dim]")
    if kept_rejected:
        console.print(f"[yellow]kept (rejected)[/yellow] {len(kept_rejected)} behavior(s) you "
                      f"rejected as regressions: {escape(', '.join(kept_rejected))}\n  fix the "
                      f"code, or override with `nightward approve <name>`.", soft_wrap=True)
    if doubts:
        console.print(f"[yellow]kept[/yellow] {len(held)} REMOVED behavior(s) in the baseline "
                      f"that this run can't prove gone:")
        for n, why in doubts.items():
            console.print(f"  - {escape(n)}: {escape(why)}", soft_wrap=True)
        console.print("  if a removal is intended, accept it with `nightward approve <name>`.")
    elif held:
        console.print(f"[yellow]kept[/yellow] {len(held)} REMOVED behavior(s) in the baseline: "
                      f"{escape(', '.join(held))}\n  removals may come from skipped tests or "
                      f"a partial path. Accept them with `nightward approve <name>` or "
                      f"`--all --include-removed`.", soft_wrap=True)
    _print_summary(recompute(store, judge=judge))


@app.command()
@handle_errors
def reject(name: str, dir: str = typer.Option(DEFAULT_DIR)):
    """Confirm a change as a real regression. Boundary stays breached.

    `approve --all` will skip it while the same payload is pending; an explicit
    `approve <name>` overrides and clears the rejection.
    """
    store = _existing_store(dir)
    with store_lock(store.root, "nightward reject"):
        store.mark_rejected(name)
    console.print(f"[red]rejected[/red] {escape(name)} - boundary stays breached. "
                  f"Fix the code and re-run `nightward run`.")
    console.print(f"[dim]commit {escape(str(store.rejected_dir))}/ with your change: "
                  f"a rejection protects every clone that has it[/dim]", soft_wrap=True)


# doctor's marks: ~ noise with a remedy, * looks real, ! shape changed.
_DOCTOR_MARKS = {"volatile": "~", "float-noise": "~", "order-only": "~",
                 "content-hash": "*", "changed": "*", "structural": "!"}
_DOCTOR_LINES = 20  # per behavior; the rest is summarized


@app.command()
@handle_errors
def doctor(names: list[str] | None = NAMES_ARG,
           group: list[str] | None = GROUP_OPT,
           dir: str = typer.Option(DEFAULT_DIR)):
    """Explain what moved in CHANGED behaviors; suggest scrub rules only for
    values that are volatile by evidence (timestamps, random tokens)."""
    from .core.doctor import diagnose
    store = _existing_store(dir)
    pending = store.load_pending()
    if not pending:
        raise NightwardError("no pending capture - run `nightward run` first")
    wanted = (_scope(names, group, {n: b.group or "(ungrouped)" for n, b in pending.items()},
                     "in the current capture") if names or group else None)
    diag = diagnose(store.load_baseline(), pending, only=wanted)
    if not diag["changed"]:
        scope = " in that selection" if wanted is not None else ""
        console.print(f"[green]no CHANGED behaviors{scope} - nothing to diagnose[/green]")
        return
    looks_real = False
    for name, found in diag["behaviors"].items():
        console.print(f"\n[bold]{escape(name)}[/bold]")
        for f in found[:_DOCTOR_LINES]:
            mark = _DOCTOR_MARKS[f["kind"]]
            looks_real |= mark != "~"
            count = f" ({f['count']} values)" if f["count"] > 1 else ""
            detail = f"  {f['detail']}" if f["detail"] else ""
            console.print(f"  {mark} {escape(f['path'])}{count}  [dim]{escape(f['note'])}"
                          f"{escape(detail)}[/dim]", soft_wrap=True)
        if len(found) > _DOCTOR_LINES:
            console.print(f"  [dim]... {len(found) - _DOCTOR_LINES} more path(s)[/dim]")
    if diag["suggestions"]:
        console.print("\n[bold]volatile by evidence[/bold] - if this is noise, tame it in "
                      "conftest.py:")
        console.print("  [cyan]from nightward import scrub[/cyan]")
        for s in diag["suggestions"]:
            console.print(f"  [cyan]{escape(s['rule'])}[/cyan]  [dim]# {escape(s['reason'])}"
                          f"[/dim]", soft_wrap=True)
        console.print("then re-run [cyan]nightward run[/cyan] and review what is left.")
        if any("register_field" in s["rule"] for s in diag["suggestions"]):
            console.print("[yellow]caution:[/yellow] register_field masks that key in "
                          "[bold]every[/bold] behavior - doctor offers it only for keys "
                          "that are not stable anywhere else in this capture.")
    if looks_real:
        console.print("\n[bold]*[/bold] / [bold]![/bold] look like real changes: "
                      "`nightward review`, then approve or fix - never scrub a regression. "
                      "doctor calls a value volatile only when the value shows it. If one of "
                      "these changes again on a re-run with no code edits, it is volatile: "
                      "mask it at capture time in that test.", soft_wrap=True)


@app.command()
@handle_errors
def gate(dir: str = typer.Option(DEFAULT_DIR)):
    """Exit 0 if the boundary is intact, 1 otherwise (for CI / agent loops)."""
    store = _existing_store(dir)
    report = _require_report(store)
    if is_stale(store, report):
        console.print(STALE_MESSAGE)
        raise typer.Exit(1)
    if report.get("incomplete"):
        console.print(f"[red]last run incomplete:[/red] {_incomplete_text(report['incomplete'])}")
        raise typer.Exit(1)
    # The verdict is the last run's; code edited since then is not in it (R1-LLM-07).
    as_of = f" [dim]{escape(_as_of(report.get('generated_at')))}[/dim]"
    if report.get("boundary") == "intact":
        console.print(f"[green]boundary intact[/green]{as_of}")
        raise typer.Exit(0)
    console.print(f"[red]boundary breached[/red] ({report.get('unapproved', 0)} "
                  f"unapproved){as_of}")
    raise typer.Exit(1)


@app.command()
@handle_errors
def view(dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir to read"),
         out: str = typer.Option(DEFAULT_SITE, help="Output directory for the static site"),
         serve: bool = typer.Option(True, "--serve/--no-serve",
                                    help="Serve locally and open a browser after building"),
         port: int = typer.Option(8000, help="Port for --serve"),
         open_browser: bool = typer.Option(True, "--open/--no-open",
                                           help="Open a browser when serving")):
    """Build a static, read-only blast-radius dashboard (view it in a browser)."""
    store = _existing_store(dir)
    out_path = build_site(Path(dir), Path(out))
    report = store.load_report()
    if not is_stale(store, report):
        _mark_reviewed(store, report, "view")
    console.print(f"[green]built[/green] {escape(str(out_path))}/ "
                  "(index.html, app.js, style.css, data.json)")
    _warn_unless_ignored(out_path / "data.json", "it holds your captured behaviors")
    if serve:
        from .view.serve import serve as _serve
        _serve(out_path, port=port, open_browser=open_browser)
    else:
        console.print(f"open it with:  [cyan]python -m http.server -d "
                      f"{escape(str(out_path))} {port}[/cyan]  "
                      "(fetch needs http, not file://)")


@app.command()
@handle_errors
def status(dir: str = typer.Option(DEFAULT_DIR),
           json_: bool = typer.Option(False, "--json", help="Machine-readable output")):
    """Print boundary status - the stop-condition signal for agent loops."""
    store = _store(dir)
    if not store.root.is_dir():
        # Still "unknown" (a loop polling a fresh checkout must not crash), but
        # say where we looked: a typo'd --dir is otherwise indistinguishable.
        err_console.print(f"[yellow]note:[/yellow] {escape(_missing_store_message(dir))}",
                          soft_wrap=True)
    report = store.load_report()
    payload = status_payload(report, stale=is_stale(store, report))
    if json_:
        print(json.dumps(payload, ensure_ascii=False), file=_stdout)
    else:
        _print_status(payload)


def _as_of(generated_at: str | None) -> str:
    return (f"(as of the last run, {generated_at}; re-run `nightward run` after code "
            f"edits)")


_STATUS_COLORS = {"intact": "green", "breached": "red", "incomplete": "red",
                  "stale": "red", "unknown": "yellow"}


def _print_status(payload: dict) -> None:
    """Human form of status_payload (the --json shape is the machine contract)."""
    boundary = payload["boundary"]
    head = f"[{_STATUS_COLORS.get(boundary, 'yellow')}]boundary {escape(boundary)}[/]"
    if boundary == "unknown":
        console.print(f"{head} - no report yet; run `nightward run`")
        return
    if payload["unapproved"]:
        head += f" ({payload['unapproved']} unapproved)"
    console.print(head)
    if boundary == "stale":
        console.print("the baseline or the capture changed since the last report; "
                      "re-run `nightward run`")
    if payload.get("incomplete"):
        console.print(f"capture incomplete: {_incomplete_text(payload['incomplete'])}")
    for ch in payload["changes"]:
        console.print(f"  - [[cyan]{ch['kind']}[/cyan]] {escape(ch['name'])} "
                      f"[dim]({escape(ch.get('group') or '(ungrouped)')})[/dim]")
    if payload.get("judged_same"):
        console.print(f"[dim]{len(payload['judged_same'])} change(s) ruled semantically "
                      f"SAME by the judge - audit with `nightward review`[/dim]")
    console.print(f"[dim]{escape(_as_of(payload.get('generated_at')))}[/dim]")


@app.command("mcp")
@handle_errors
def mcp_cmd(judge: str | None = typer.Option(
        None, help="Semantic judge for nightward_run, as provider:model. The agent "
                   "can't choose it. Default: $NIGHTWARD_JUDGE, else the last run's judge")):
    """Start the MCP server (stdio) for AI agents - exposes run/status, NOT approve."""
    from .mcp_server import serve
    serve(judge=judge)


if __name__ == "__main__":
    app()
