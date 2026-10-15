"""Shared CLI plumbing: the app, consoles, store helpers and the verdict printout."""
from __future__ import annotations

import errno
import functools
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from .. import __version__, shellquote
from ..core.baseline import Store, change_token
from ..core.behavior import env_text
from ..errors import NightwardError
from ..runner import is_stale
from ..runner import store_above as _store_above

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

class _App(typer.Typer):
    """Typer app that takes arguments literally on Windows too.

    Click expands $VAR, %VAR%, ~ and globs in sys.argv on Windows (cmd.exe
    doesn't), after the shell's quoting: a quoted 'price.$region' would reach
    approve as another behavior's name (R3-WEB-02). Names are never paths.
    """

    def __call__(self, *args, **kwargs):
        kwargs.setdefault("windows_expand_args", False)
        try:
            return super().__call__(*args, **kwargs)
        except OSError as exc:
            # Click/rich write --help and usage straight to sys.stdout, not
            # through _stdout: `nightward --help | head` lands here (R4-FIN-04).
            if exc.errno not in (errno.EPIPE, errno.EINVAL):
                raise
            _stdout._gone(exc)
            raise SystemExit(0) from None


app = _App(
    help="nightward - regression firewall for AI-driven changes",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(file=_stdout, legacy_windows=False)


def _print_version(value: bool) -> None:
    if value:
        print(f"nightward {__version__}", file=_stdout)
        raise typer.Exit()


@app.callback()
def _main(version: bool = typer.Option(
        False, "--version", callback=_print_version, is_eager=True,
        help="Show the nightward version and exit")):
    """nightward - regression firewall for AI-driven changes"""
err_console = Console(stderr=True, legacy_windows=False)

DEFAULT_DIR = ".nightward"
DEFAULT_SITE = "nightward-site"   # `view` output: holds captured data, never commit it

# Store entries that are per-run state, relative to the store dir.
# judge/ (the ledger) and rejected/ are deliberately NOT here: the verdict
# ledger keeps judged-SAME boundaries deterministic on fresh clones / CI, and a
# rejection must protect every clone, not just the machine that made it (D17).
TRANSIENT_ENTRIES = ("pending/", "report.json", "run_meta.json",
                     "pending.tmp/", "**/*.tmp", ".lock", ".lock.takeover", "reviewed.json")
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
            # soft_wrap: a command in the message must stay copy-pasteable
            err_console.print(f"[red]error:[/red] {escape(str(exc))}", soft_wrap=True)
            raise typer.Exit(2) from None
    return wrapper


def _store(dir_: str) -> Store:
    return Store(Path(dir_))


def _check_dir(dir_: str) -> None:
    p = Path(dir_)
    if p.exists() and not p.is_dir():
        raise NightwardError(f"--dir {dir_!r} exists but is not a directory")


def _missing_store_message(dir_: str) -> str:
    above = _store_above(dir_)
    if above:
        return (f"no nightward store at '{dir_}', but found '{above}' - run from the "
                f"project root, or pass --dir {above}")
    return (f"no nightward store at '{dir_}' (under {Path.cwd()}) - check --dir, or "
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
        raise NightwardError("no report - run `nightward run` (a run that aborted, was refused "
                             "or timed out invalidates the last report)")
    return report


def _incomplete_text(incomplete: dict) -> str:
    return (f"{incomplete.get('failed', 0)} failed, {incomplete.get('errors', 0)} error(s) "
            f"in the capture run - fix them and re-run `nightward run`")


def _incomplete_short(incomplete: dict) -> str:
    parts = [f"{incomplete[k]} {word} capture test(s)"
             for k, word in (("failed", "failed"), ("errors", "errored")) if incomplete.get(k)]
    return ", ".join(parts)


STALE_MESSAGE = ("[red]report is stale[/red] - the baseline, the capture or a rejection "
                 "changed since the last report; re-run `nightward run` (or `nightward "
                 "report` after `pytest --nightward-record`)")


def _report_items(report: dict) -> list[dict]:
    """Every change a report can show: the blast radius, then the judged-SAME rulings."""
    return ([it for items in report.get("blast_radius", {}).values() for it in items]
            + list(report.get("judged_same") or []))


def _blast_names(report: dict) -> set[str]:
    return {it["name"] for items in report.get("blast_radius", {}).values() for it in items}


def _mark_reviewed(store: Store, report: dict | None, via: str,
                   names: set[str] | None = None) -> None:
    """Record the changes a human was just shown, so approve and reject act on
    exactly those (D10, D19). names: what was displayed (None: everything).

    Only the surfaces that display diffs call this: `review` (what it printed)
    and `view` (the dashboard) - never `run`/`report`, which list names only
    (D26), and never MCP: an agent's run between review and approve must not
    choose what an approval covers.
    """
    if not report or not report.get("pending_digest"):
        return
    items = _report_items(report)
    shown = {it["name"]: it["token"] for it in items
             if it.get("token") and (names is None or it["name"] in names)}
    store.mark_reviewed(shown, via, {it["name"]: it.get("token") for it in items})


def _fresh_report(store: Store, verb: str) -> dict:
    """The report a decision acts on; refused when it no longer describes the store."""
    report = store.load_report()
    if report is None:
        raise NightwardError(f"no report - run `nightward run` (a run that aborted or was "
                             f"refused invalidates the last report), review it, then {verb}")
    if is_stale(store, report):
        # e.g. a `git pull` brought a teammate's baseline or rejection: the
        # human decided on a report that no longer describes the store
        # (R3-OPS-03, R4-FIN-03).
        raise NightwardError(f"the baseline, the capture or a rejection changed since the last "
                             f"report (a `git pull`/checkout, or another run) - run `nightward "
                             f"run`, review again, then {verb}")
    return report


def _shown(names: list[str], limit: int = 5) -> str:
    more = f" and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


def _check_reviewed(store: Store, names: list[str], baseline, pending, verb: str) -> None:
    """Refuse unless the human was shown each of `names` in its current state (D19)."""
    if not names:
        return
    seen = store.load_reviewed().get("seen")
    if seen is None:
        raise NightwardError(f"this capture is not reviewed yet - only `nightward review` (or "
                             f"`nightward view`) marks changes reviewed; `run` lists names "
                             f"only. Run `nightward review`, then {verb}")
    changed = [n for n in names
               if n in seen and seen[n] != change_token(baseline.get(n), pending.get(n))]
    unseen = [n for n in names if n not in seen]
    if not changed and not unseen:
        return
    parts = []
    if changed:
        # Name the run the store knows took the current capture; never guess who.
        meta = store.load_run_meta()
        by, at = meta.get("captured_by"), meta.get("captured_at")
        how = (f"the capture now in the store was taken by `{by}` at {at}" if by and at
               else "a later run captured again")
        parts.append(f"{_shown(changed)} changed since your review ({how})")
    if unseen:
        parts.append(f"{_shown(unseen)}: not reviewed yet (your last review did not show it; "
                     f"only `review`/`view` mark changes reviewed)")
    todo = changed + unseen
    cmd = (shellquote.command("review", todo) if len(todo) <= 5 else None) or "nightward review"
    raise NightwardError("; ".join(parts) + f". Run `{cmd}`, then {verb} what it shows.")


def _git(*args: str, input: str | None = None) -> subprocess.CompletedProcess | None:
    """Run git; None when git is missing or hangs.

    git writes UTF-8 whatever the console code page: decoding with the locale
    (cp949 on Korean Windows) garbles or crashes on a name like "José" or a
    Hangul user.name (R4-FIN-02).
    """
    try:
        return subprocess.run(["git", *args], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=10, input=input,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _who() -> str:
    """Who is deciding: git's user.name, else the OS login (recorded with a rejection)."""
    r = _git("config", "user.name")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # no login name is not an error
        return ""


def _legacy_rejection_note(names: list[str]) -> None:
    if names:
        err_console.print(
            f"[yellow]note:[/yellow] {len(names)} rejection record(s) predate binding "
            f"rejections (no reviewer recorded) and are audit-only - they never breach or "
            f"block: {escape(_shown(names))}. Delete them, or `nightward reject` again to "
            f"make a rejection binding", soft_wrap=True)


def _rejected_note(it: dict) -> str:
    if not it.get("rejected"):
        return ""
    by = f" by {escape(it['rejected_by'])}" if it.get("rejected_by") else ""
    return f" [red](rejected{by})[/red]"


def _shown_path(path: Path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:   # another drive on Windows
        return str(path)


def _names_text(names: list[str], limit: int = 5) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more"
                                       if len(names) > limit else "")


def _scrub_rule_line(rule: dict) -> str:
    behaviors = rule.get("behaviors") or []
    line = (f"{rule.get('rule')}: {rule.get('values', 0)} value(s) in {len(behaviors)} "
            f"behavior(s)")
    if not rule.get("placeholder", True):
        line += " - not a <PLACEHOLDER>: it writes a plausible value, review it"
    return line


def _print_scrub_rules(rules: list[dict]) -> None:
    """The custom scrub rules behind the last capture (D28, R5-POL-03): a value
    they replaced never reaches the comparison, so every review surface says so."""
    if not rules:
        return
    console.print("[dim]custom scrub rules (values they replaced in the last run):[/dim]")
    for rule in rules:
        console.print(f"[dim]  - {escape(_scrub_rule_line(rule))}[/dim]", soft_wrap=True)


def _print_not_run(report: dict) -> None:
    not_run = [it["name"] for it in report.get("not_run") or []]
    if not_run:
        console.print(f"[yellow]{len(not_run)} behavior(s) not checked[/yellow] (their test "
                      f"was deselected with -k/-m): {escape(_shown(not_run))} - a run "
                      f"without the selection checks them", soft_wrap=True)


def _print_env_mismatch(report: dict) -> None:
    """Say when changed behaviors were approved on another OS or Python (a hint,
    never a verdict: the boundary stays what the fingerprints say)."""
    mismatch = report.get("env_mismatch")
    if not mismatch:
        return
    by_env: dict[str, list[str]] = {}
    for name, env in sorted(mismatch["behaviors"].items()):
        by_env.setdefault(env_text(env), []).append(name)
    approved = "; ".join(f"{escape(_names_text(names, 3))} on {escape(where)}"
                         for where, names in by_env.items())
    err_console.print(
        f"[yellow]note:[/yellow] {len(mismatch['behaviors'])} changed behavior(s) were "
        f"approved on another environment ({approved}); this capture ran on "
        f"{escape(env_text(mismatch['capture']))}. A path, a line ending or an OS error "
        f"message can move a payload with no code change - check that before the code "
        f"(docs/payload-design.md)", soft_wrap=True)


def _print_summary(report: dict) -> None:
    c = report["counts"]
    # Same word as status --json and the dashboard: a run with failing capture
    # tests is never "intact", even when nothing captured moved (R2-DATA-03).
    incomplete = report.get("incomplete")
    gap = (_incomplete_short(incomplete) if incomplete else "")
    if report["boundary"] in ("intact", "partial") and incomplete:
        console.print(f"\n[bold]Boundary:[/bold] [red]incomplete[/red] ({gap})")
    elif report["boundary"] == "intact":
        console.print("\n[bold]Boundary:[/bold] [green]intact[/green]")
    elif report["boundary"] == "partial":
        # Nothing unapproved among what ran - but not everything ran (D23).
        console.print(f"\n[bold]Boundary:[/bold] [yellow]partial[/yellow] "
                      f"({len(report.get('not_run') or [])} not checked - not done)")
    else:
        console.print(f"\n[bold]Boundary:[/bold] [red]breached[/red] "
                      f"({report['unapproved']} unapproved{'; ' + gap if gap else ''})")
    console.print(f"unchanged={c['unchanged']} changed={c['changed']} "
                  f"new={c['new']} removed={c['removed']}")
    _print_not_run(report)
    if c.get("judged_same"):
        # Listed, not just counted: a wrong SAME is a hole in the gate (D22).
        same = [it["name"] for it in report.get("judged_same") or []]
        console.print(f"[dim]{c['judged_same']} fingerprint mismatch(es) ruled "
                      f"semantically SAME by the judge: {escape(_names_text(same))} - "
                      f"audit with `nightward review`[/dim]", soft_wrap=True)
    judge = report.get("judge") or {}
    if judge.get("ledger_mismatch"):
        err_console.print(
            f"[yellow]warning:[/yellow] the judge ledger's entry for "
            f"{escape(_names_text(judge['ledger_mismatch']))} did not match what "
            f"{escape(judge['spec'])} rules now (edited by hand, or recorded under older "
            f"rules); it was ruled again and rewritten - review the ledger diff",
            soft_wrap=True)
    for why in judge.get("ledger_rejected") or ():
        # A forged or hand-edited model ruling: never replayed (R4-LLM-03).
        err_console.print(
            f"[yellow]warning:[/yellow] judge ledger entry {escape(why)} - not replayed; "
            f"ruled again, or compared exactly if the judge is unavailable. Review "
            f"the ledger diff", soft_wrap=True)
    if judge.get("unavailable") and not judge.get("spec"):
        err_console.print(
            f"[yellow]note:[/yellow] {len(judge['compared_exactly'])} approved semantic "
            f"behavior(s) compared exactly: {escape(judge['unavailable'])}", soft_wrap=True)
    elif judge.get("unavailable"):
        err_console.print(
            f"[yellow]warning:[/yellow] judge {escape(judge['spec'])} unavailable "
            f"({escape(judge['unavailable'])}); {len(judge['compared_exactly'])} semantic "
            f"behavior(s) compared exactly", soft_wrap=True)
    for group, items in report.get("blast_radius", {}).items():
        console.print(f"\n[yellow]group: {escape(group)}[/yellow]")
        for it in items:
            judged = (f" [dim](judged DIFFERENT by {escape(it['judge_model'])})[/dim]"
                      if it.get("judged") else "")
            console.print(f"  - [[cyan]{it['kind']}[/cyan]] {escape(it['name'])}{judged}"
                          f"{_rejected_note(it)}")
    _print_env_mismatch(report)

