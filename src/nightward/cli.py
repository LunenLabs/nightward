"""nightward CLI - init / run / review / doctor / approve / reject / gate / status."""
from __future__ import annotations

import errno
import functools
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from . import __version__, shellquote
from .config import judge_setting
from .core.baseline import Store, change_token
from .core.diff import NOT_RUN, REMOVED, UNCHANGED
from .core.lock import store_lock
from .errors import NightwardError
from .runner import (
    classify,
    execute_run,
    is_stale,
    judge_from_meta,
    recompute,
    recompute_capture,
    refused_run_invalidates,
    standing_rejections,
)
from .runner import store_above as _store_above
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

class _App(typer.Typer):
    """Typer app that takes arguments literally on Windows too.

    Click expands $VAR, %VAR%, ~ and globs in sys.argv on Windows (cmd.exe
    doesn't), after the shell's quoting: a quoted 'price.$region' would reach
    approve as another behavior's name (R3-WEB-02). Names are never paths.
    """

    def __call__(self, *args, **kwargs):
        kwargs.setdefault("windows_expand_args", False)
        return super().__call__(*args, **kwargs)


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


STALE_MESSAGE = ("[red]report is stale[/red] - the baseline or the capture changed since "
                 "the last report; re-run `nightward run` (or `nightward report` after "
                 "`pytest --nightward-record`)")


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

    Only human surfaces call this (run/report/review/view) - never MCP: an
    agent's run between review and approve must not choose what an approval
    covers.
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
        # e.g. a `git pull` brought a teammate's baseline: the changes the human
        # reviewed are not the changes on disk any more (R3-OPS-03).
        raise NightwardError(f"the baseline or the capture changed since the last report (a "
                             f"`git pull`/checkout, or another run) - run `nightward run`, "
                             f"review again, then {verb}")
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
        raise NightwardError(f"no human has reviewed this capture yet - run `nightward "
                             f"review` (or `nightward run` / `nightward view`), then {verb}")
    changed = [n for n in names
               if n in seen and seen[n] != change_token(baseline.get(n), pending.get(n))]
    unseen = [n for n in names if n not in seen]
    if not changed and not unseen:
        return
    parts = []
    if changed:
        parts.append(f"{_shown(changed)} changed since you last reviewed it (another run - "
                     f"an agent's nightward_run, CI, a teammate - captured again)")
    if unseen:
        parts.append(f"{_shown(unseen)} was not shown in your last review")
    todo = changed + unseen
    cmd = (shellquote.command("review", todo) if len(todo) <= 5 else None) or "nightward review"
    raise NightwardError("; ".join(parts) + f". Run `{cmd}`, then {verb} what it shows.")


def _who() -> str:
    """Who is deciding: git's user.name, else the OS login (recorded with a rejection)."""
    try:
        r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    import getpass
    try:
        return getpass.getuser()
    except Exception:  # no login name is not an error
        return ""


def _rejected_note(it: dict) -> str:
    if not it.get("rejected"):
        return ""
    by = f" by {escape(it['rejected_by'])}" if it.get("rejected_by") else ""
    return f" [red](rejected{by})[/red]"


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


# What git must commit (the boundary and the decisions on it) and what it must
# not (per-run state), as probe paths under the store (R3-FIN-06, R3-OPS-04).
_COMMITTED_PROBES = (
    ("baseline/nightward-probe.approved.json",
     "the approved baseline ({store}/baseline/) is git-ignored by `{rule}`: it can't be "
     "committed, so CI and teammates see every behavior as NEW - remove that rule "
     "(nightward's own rules ignore only the per-run files)"),
    ("rejected/nightward-probe.rejected.json",
     "rejections ({store}/rejected/) are git-ignored by `{rule}`: they won't reach other "
     "clones - {fix}"),
    ("judge/nightward-probe.json",       # the ledger: one file per ruling (D22)
     "the judge ledger ({store}/judge/) is git-ignored by `{rule}`: fresh clones and CI "
     "can't replay its rulings - remove that rule"),
)
# The single-file ledger older versions wrote: still read, so still committed.
_LEGACY_LEDGER_PROBE = (
    "judge_verdicts.json",
    "the legacy judge verdict ledger ({store}/judge_verdicts.json) is git-ignored by "
    "`{rule}`: fresh clones and CI can't replay its rulings - remove that rule")
_TRANSIENT_PROBES = ("report.json", "run_meta.json", "reviewed.json", ".lock",
                     "pending/nightward-probe.received.json")


def _ignore_rules(paths: list[str]) -> dict[str, str] | None:
    """{path: "source:line:pattern"} for each of `paths` git ignores (None: no git)."""
    try:
        r = subprocess.run(["git", "check-ignore", "-v", "--no-index", "--", *paths],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           stdin=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode not in (0, 1):     # not a repository, or the store is outside it
        return None
    rules = {}
    for line in r.stdout.splitlines():
        meta, _, path = line.partition("\t")
        if meta.rsplit(":", 1)[-1].startswith("!"):
            continue                   # a negation: matched, but not ignored
        rules[path] = meta
    return rules


def _ignore_problems(dir_: str) -> list[str]:
    """What the .gitignore rules get wrong for this store: committed entries that
    are ignored (an old or overbroad rule), or per-run files that are not."""
    root = Path(dir_)
    probes = list(_COMMITTED_PROBES)
    if (root / _LEGACY_LEDGER_PROBE[0]).exists():
        probes.append(_LEGACY_LEDGER_PROBE)
    committed = {(root / p).as_posix(): msg for p, msg in probes}
    transient = {(root / p).as_posix(): p.split("/")[0] + ("/" if "/" in p else "")
                 for p in _TRANSIENT_PROBES}
    rules = _ignore_rules([*committed, *transient])
    if rules is None:
        return []
    problems, named = [], set()
    for p, msg in committed.items():
        rule = rules.get(p)
        if rule is None or rule in named:   # one overbroad rule: say it once
            continue
        named.add(rule)
        # Only the rejected/ line older `init`s wrote is init's to remove.
        fix = ("re-run `nightward init` to update .gitignore"
               if rule.endswith(f"{root.as_posix()}/rejected/") else "remove that rule")
        problems.append(msg.format(store=root.as_posix(), rule=rule, fix=fix))
    loose = [name for p, name in transient.items() if p not in rules]
    if loose:
        problems.append(f"per-run files are not git-ignored ({', '.join(loose)}) and must not "
                        f"be committed - re-run `nightward init` to add the .gitignore rules")
    return problems


def _warn_ignore_problems(dir_: str) -> None:
    for problem in _ignore_problems(dir_):
        err_console.print(f"[yellow]warning:[/yellow] {escape(problem)}", soft_wrap=True)


def _warn_unless_ignored(path: Path, what: str,
                         fix: str = "run `nightward init` to add the .gitignore rules") -> None:
    # Only a nudge: a rule in a parent .gitignore or info/exclude counts too.
    if _git_ignored(path) is False:
        err_console.print(f"[yellow]warning:[/yellow] {escape(str(path))} is not git-ignored "
                          f"and {what} - {escape(fix)}", soft_wrap=True)


def _ignore_fix(out_path: Path) -> str:
    """How to ignore a dashboard dir: init knows only the default one (R1-WEB-06)."""
    rule = _store_prefix(str(out_path))
    if rule == DEFAULT_SITE:
        return "run `nightward init` to add the .gitignore rules"
    return f"add `{rule or out_path.as_posix()}/` to .gitignore"


def _shown_path(path: Path) -> str:
    try:
        return os.path.relpath(path)
    except ValueError:   # another drive on Windows
        return str(path)


def _names_text(names: list[str], limit: int = 5) -> str:
    return ", ".join(names[:limit]) + (f" and {len(names) - limit} more"
                                       if len(names) > limit else "")


def _print_not_run(report: dict) -> None:
    not_run = [it["name"] for it in report.get("not_run") or []]
    if not_run:
        console.print(f"[yellow]{len(not_run)} behavior(s) not checked[/yellow] (their test "
                      f"was deselected with -k/-m): {escape(_shown(not_run))} - a run "
                      f"without the selection checks them", soft_wrap=True)


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


@app.command()
@handle_errors
def init(dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir")):
    """Create the nightward store and add ignore rules to .gitignore."""
    _check_dir(dir)
    store = _store(dir)
    existed = store.root.is_dir()
    store.ensure()
    approved = len(store.load_baseline()) if existed else 0
    if existed:
        # e.g. a fresh clone with a committed baseline: nothing was created (R1-WEB-06).
        console.print(f"store exists: {escape(store.root.as_posix())}/ ({approved} approved "
                      f"behavior(s))")
    else:
        console.print(f"[green]created[/green] {escape(store.root.as_posix())}/ "
                      f"(baseline, pending)")

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
    # A rule init doesn't own (e.g. a blanket `.nightward/`) can still hide the
    # baseline from git; say which rule (R3-OPS-04).
    for problem in _ignore_problems(dir):
        if not problem.startswith("per-run files"):
            err_console.print(f"[yellow]warning:[/yellow] {escape(problem)}", soft_wrap=True)
    if approved:
        # Approving everything would bury whatever moved since the baseline.
        console.print("\nNext: `nightward run <path>` to gate the code against the approved "
                      "baseline, then `nightward review` what moved.")
    else:
        console.print("\nNext: capture behaviors with the `behavior` pytest fixture, "
                      "then `nightward run <path>`, `nightward review` and `nightward "
                      "approve --all`.")
    # Rejections and judge rulings are decisions too: without them a clone or
    # CI gates differently from the machine that made them (R4-FIN-05).
    ignored = " (the per-run files are git-ignored)" if _gitignore_lines(dir) else ""
    console.print(f"Commit {escape(store.root.as_posix())}/ with your code: baseline/, "
                  f"rejected/ and judge/ are the team's decisions{ignored}.", soft_wrap=True)


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
@handle_errors
def run(ctx: typer.Context,
        path: str = typer.Argument(".", help="Path passed to pytest"),
        dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir"),
        judge: str | None = typer.Option(
            None, help="Semantic judge for semantic=True behaviors, as provider:model "
                       "(e.g. anthropic:claude-haiku-4-5, persona:editor), for this run "
                       "only. Default: $NIGHTWARD_JUDGE, else [tool.nightward] judge "
                       "in the pyproject.toml nearest the store")):
    """Re-run tests, capture behaviors, compute the blast radius.

    Extra pytest arguments go after `--`: nightward run tests -- -m "not gpu" -p no:randomly
    (without a path, `nightward run -- -k clamp` runs "."; a run with extra pytest
    arguments never proves a removal).
    """
    extra = [a for a in ctx.args if a != "--"]
    if path.startswith("-"):
        # `nightward run -- -k clamp`: click hands the first pytest arg to PATH,
        # which is optional (R3-OPS-05).
        path, extra = ".", [path, *extra]
    _check_dir(dir)
    if dir == DEFAULT_DIR and not Path(dir).exists() and _store_above(dir):
        # pytest finds the rootdir from anywhere; a second store here would
        # report every behavior as NEW (R1-OPS-07).
        raise NightwardError(_missing_store_message(dir))
    # A refused run (bad judge config, busy lock, ...) invalidates the last
    # report like an aborted one: the code it described may have changed.
    with refused_run_invalidates(dir):
        # --judge and $NIGHTWARD_JUDGE are per-run overrides; the committed
        # [tool.nightward] judge is the project's decision (D14).
        if judge:
            source = "--judge, this run only"
        elif os.environ.get("NIGHTWARD_JUDGE"):
            judge, source = os.environ["NIGHTWARD_JUDGE"], "$NIGHTWARD_JUDGE, this run only"
        else:
            # The project that owns the store decides, whichever tests run (D22).
            judge, where = judge_setting(dir)
            source = _shown_path(where) if where else "pyproject.toml"
        console.print(f"[dim]$ pytest {escape(shlex.join([path, *extra]))} --nightward-record "
                      f"--nightward-dir {escape(dir)}[/dim]", soft_wrap=True)
        if judge:
            console.print(f"[dim]judge: {escape(judge)} ({source})[/dim]")
        result = execute_run(path, dir, judge_spec=judge, pytest_args=extra)
    # Deselected tests are "not checked" (D21), listed on their own below.
    not_run = [f"{result[k]} {k}" for k in ("skipped", "xfailed") if result[k]]
    if not_run and result["report"]["counts"].get("removed"):
        err_console.print(f"[yellow]warning:[/yellow] {', '.join(not_run)} test(s) - "
                          "behaviors they capture appear as REMOVED; blast radius may show "
                          "false positives")
    scrubbed = result["scrubbed"]
    if scrubbed["values"] and scrubbed.get("changed", True):
        # Masking is noise control, but it can also hide a real datetime change.
        # Said again only when the set of masked behaviors changes.
        names = scrubbed.get("names") or []
        shown = ", ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
        err_console.print(f"[dim]note: default scrubbers masked {scrubbed['values']} value(s) "
                          f"in {scrubbed['behaviors']} behavior(s) (timestamps/uuids)"
                          f"{': ' + escape(shown) if shown else ''} - opt out with "
                          f"scrub=False[/dim]", soft_wrap=True)
    for rule in result["scrub_unmatched"]:
        # The user believes this noise is handled; it isn't (R1-WEB-03).
        why = ("no captured dict has that key" if rule.startswith("register_field(") else
               "patterns see the canonical JSON text, where a '\"' inside a string is "
               "written '\\\"' and a newline '\\n' - see `help(nightward.scrub.register)`")
        err_console.print(f"[yellow]warning:[/yellow] scrub rule {escape(rule)} matched "
                          f"nothing in this run ({escape(why)})", soft_wrap=True)
    _print_summary(result["report"])
    _mark_reviewed(_store(dir), result["report"], "run", _blast_names(result["report"]))
    # A committed report.json lets a CI `gate` without `run` pass on an old verdict.
    # A committed report.json lets a CI `gate` pass on an old verdict; an
    # ignored baseline or rejected/ never reaches CI or teammates.
    _warn_ignore_problems(dir)
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
    _mark_reviewed(store, report, "report", _blast_names(report))
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
                  f"`{escape(shellquote.command('review', [it['name']]) or '')} "
                  f"--max-lines 0`[/dim]",
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
    _mark_reviewed(store, report, "review", wanted)
    outside = sorted(_blast_names(report) - wanted)
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
            console.print(f"\n[bold][[cyan]{it['kind']}[/cyan]] {escape(it['name'])}[/bold]"
                          f"{_rejected_note(it)}")
            if it.get("rejected"):
                console.print("[dim]this exact payload is a standing rejection "
                              "(.nightward/rejected/); approving it overrides that "
                              "decision[/dim]")
            if it.get("judged"):
                console.print(f"[dim]judged DIFFERENT by {escape(it['judge_model'])}"
                              f"{_REPLAYED if it.get('judge_replayed') else ''}: "
                              f"{escape(it.get('judge_reason', ''))}[/dim]")
            _print_diff(it, max_lines)
    if outside:
        # A scoped review covers only what it showed (R3-WEB-01).
        console.print(f"\n[yellow]{len(outside)} other unapproved change(s) outside this "
                      f"selection, not reviewed:[/yellow] {escape(_shown(outside))}",
                      soft_wrap=True)
    if judged_same:
        # Outside the boundary, but a wrong SAME is a hole in the gate: show the
        # exact wording the judge accepted so a human can audit it (R1-LLM-04).
        console.print(f"\n[yellow]ruled semantically SAME by the judge[/yellow] "
                      f"({len(judged_same)}) - not in the boundary; audit the wording:")
        for it in judged_same:
            console.print(f"\n[bold][[cyan]SAME[/cyan]] {escape(it['name'])}[/bold] "
                          f"[dim]{escape(it.get('judge_model', ''))}"
                          f"{_REPLAYED if it.get('judge_replayed') else ''}: "
                          f"{escape(it.get('judge_reason', ''))}[/dim]")
            _print_diff(it, max_lines)


# A model's ruling replayed from the committed ledger was not made this run:
# whoever last edited the ledger made it (D22).
_REPLAYED = " (replayed from the committed ledger, not ruled this run)"


def _removal_doubt(name: str, b, meta: dict) -> str | None:
    """Why the last run can't prove a REMOVED behavior gone, or None (D18).

    Inferring proof from partial runs kept leaking (shifted parametrize ids,
    captures moved into skipped tests, --collect-only, --ignore), so only a
    clean whole-suite run proves a removal (see pytest_plugin._scope), and
    then only if the behavior's recorded test ran and passed under that exact
    id. Baselines without a recorded test follow the same run rule.
    """
    if "clean" not in meta:
        return ("the last run recorded no removal evidence (an older nightward, or no "
                "capture yet) - re-run `nightward run`")
    if not meta["clean"]:
        return (f"the last run was not a clean whole-suite run ({meta.get('clean_doubt')}) "
                f"- only `nightward run` of the whole suite with no extra pytest arguments, "
                f"and every test passing, proves a removal")
    if b.source and b.source not in set(meta.get("completed") or ()):
        return (f"its recorded test {b.source} did not run under that id (renamed, "
                f"re-parametrized or deleted?)")
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


APPROVE_NAMES_ARG = typer.Argument(
    None, help="Behavior(s) to approve. One name always applies; several are approved "
               "like --all --include-removed limited to them", show_default=False)
APPROVE_GROUP_OPT = typer.Option(
    None, "--group", help="Approve the NEW/CHANGED behaviors of this group (repeatable): "
                          "--all limited to the group, rejections kept",
    show_default=False)


@app.command()
@handle_errors
def approve(names: list[str] | None = APPROVE_NAMES_ARG,
            group: list[str] | None = APPROVE_GROUP_OPT,
            all_: bool = typer.Option(False, "--all", help="Approve every NEW/CHANGED behavior"),
            include_removed: bool = typer.Option(
                False, "--include-removed",
                help="With --all or --group, also accept REMOVED behaviors, but only after "
                     "a clean whole-suite run (no extra pytest arguments, nothing skipped, "
                     "deselected, xfailed or failing) in which their test passed "
                     "(drops them from the baseline)"),
            dir: str = typer.Option(DEFAULT_DIR)):
    """Promote pending behavior(s) into the approved baseline."""
    if sum(map(bool, (all_, names, group))) > 1:
        raise NightwardError("pick one: behavior names, --group or --all (not both)")
    store = _existing_store(dir)
    with store_lock(store.root, "nightward approve"):
        _approve(store, dir, names, all_, include_removed, groups=group)


def _group_names(store: Store, baseline, pending, judge, groups: list[str],
                 include_removed: bool) -> tuple[list[str], list[str]]:
    """(names to approve, REMOVED names left out) for `approve --group`.

    The group's unapproved behaviors, as `review --group` scopes them (standing
    rejections included, so they are kept; not-run ones excluded), minus
    removals unless --include-removed (as with --all). The command line stays
    short however big the group is (R3-DATA-06): 1,440 names don't fit in
    Windows' 32K command line, and neither does the dashboard's group chip.
    """
    known = {b.group or "(ungrouped)" for b in (*baseline.values(), *pending.values())}
    unknown = [g for g in groups if g not in known]
    if unknown:
        raise NightwardError(f"no behavior in group {', '.join(map(repr, unknown))}; "
                             f"nothing was approved")
    changes = [c for c in classify(store, baseline, pending, judge=judge, with_diff=False)
               if c.kind not in (UNCHANGED, NOT_RUN) and (c.group or "(ungrouped)") in groups]
    left_out = [] if include_removed else [c.name for c in changes if c.kind == REMOVED]
    return [c.name for c in changes if c.name not in left_out], left_out


def _approve(store: Store, dir: str, names: list[str] | None, all_: bool,
             include_removed: bool, groups: list[str] | None = None) -> None:
    baseline = store.load_baseline()
    pending = store.load_pending()
    if not baseline and not pending:
        raise NightwardError(f"nothing captured in {dir!r} yet - run `nightward run` first")
    # Approve only what the human saw, on a report that still describes the
    # store (D10, D19).
    report = _fresh_report(store, "approve")
    # Reuse the last run's judge (cached verdicts): --all then approves exactly
    # what the report lists as unapproved, and judged-SAME behaviors don't flip
    # back to CHANGED the moment something else is approved. Approving a
    # judged-SAME rewording explicitly by name still re-anchors it.
    judge = judge_from_meta(store)
    left_out: list[str] = []
    if groups:
        names, left_out = _group_names(store, baseline, pending, judge, groups,
                                       include_removed)
    # One explicit name is a human override; a group is a bulk approval at any size.
    single = bool(names) and len(names) == 1 and not groups

    held: list[str] = []
    doubts: dict[str, str] = {}
    kept_rejected: list[str] = []
    rejected = standing_rejections(store, baseline, pending)
    if all_:
        changes = [c for c in classify(store, baseline, pending, judge=judge, with_diff=False)
                   if c.kind not in (UNCHANGED, NOT_RUN)]
        removed = [c.name for c in changes if c.kind == REMOVED]
        if include_removed:
            # A test that didn't run captures nothing and looks REMOVED; approving
            # that would silently shrink the boundary. Only proven removals go.
            meta = store.load_run_meta()
            doubts = {n: why for n in removed
                      if (why := _removal_doubt(n, baseline[n], meta))}
            held = list(doubts)
        else:
            held = removed
        # A confirmed regression must never ride along with a bulk approval.
        kept_rejected = [c.name for c in changes if c.name in rejected and c.name not in held]
        targets = [c.name for c in changes if c.name not in held and c.name not in rejected]
    elif names or groups:
        # Several names (or --group, the dashboard's group chip) are a bulk
        # approval limited to them: unproven removals and standing rejections stay, as
        # with --all --include-removed; one explicit name overrides (R2-WEB-03).
        names = list(dict.fromkeys(names))
        unknown = [n for n in names if n not in pending and n not in baseline]
        if unknown:
            raise NightwardError(f"no pending or baseline behavior named "
                                 f"{', '.join(map(repr, unknown))}; nothing was approved")
        listed = {it["name"] for it in _report_items(report)}
        skipped = [n for n in names if n in {it["name"] for it in report.get("not_run") or []}]
        if skipped:
            raise NightwardError(f"{_shown(skipped)}: not checked by the last run (its test "
                                 f"was deselected) - nothing to approve; run without the "
                                 f"-k/-m selection first")
        unchanged = [n for n in names if n not in listed]
        if unchanged:
            raise NightwardError(f"{_shown(unchanged)}: unchanged in the last report - "
                                 f"nothing to approve; nothing was approved")
        if single:
            targets = names
        else:
            meta = store.load_run_meta()
            doubts = {n: why for n in names
                      if n not in pending and (why := _removal_doubt(n, baseline[n], meta))}
            held = list(doubts)
            kept_rejected = [n for n in names if n in rejected and n not in held]
            targets = [n for n in names if n not in held and n not in rejected]
            held += left_out    # --group without --include-removed: removals stay
    else:
        raise NightwardError("specify a behavior name, --group or --all")
    _check_reviewed(store, targets, baseline, pending, "approve")
    if all_:
        refreshed = _backfill_sources(store, baseline, pending)
        if refreshed:
            console.print(f"[dim]refreshed the recorded test of {refreshed} unchanged "
                          f"behavior(s) (removal evidence only)[/dim]")
    if not targets and not held and not kept_rejected:
        console.print("nothing to approve in that group" if groups
                      else "nothing to approve - boundary already intact")
        return

    for n in targets:
        if n in rejected:   # only an explicit single name gets here (R3-FIN-03)
            by = f" by {rejected[n]}" if rejected[n] else ""
            console.print(f"[yellow]overrides the rejection{escape(by)}[/yellow] of "
                          f"{escape(n)} (.nightward/rejected/{escape(n)}.rejected.json)",
                          soft_wrap=True)
        verb = _approve_one(store, n, baseline, pending)
        # The short fingerprint ties the approval to the reviewed content.
        what = f" ({pending[n].fingerprint()[:8]})" if n in pending else ""
        console.print(f"[green]{verb}[/green] {escape(n)}{what}")
        if n in rejected and store.clear_rejection(n):
            console.print(f"  [dim]cleared the rejection of {escape(n)} - commit its "
                          f"deletion with the baseline[/dim]", soft_wrap=True)
    if kept_rejected:
        console.print(f"[yellow]kept (rejected)[/yellow] {len(kept_rejected)} behavior(s) "
                      f"rejected as regressions: {escape(', '.join(kept_rejected))}\n  fix the "
                      f"code, or override with `nightward approve <name>`.", soft_wrap=True)
    if doubts:
        console.print(f"[yellow]kept[/yellow] {len(held)} REMOVED behavior(s) in the baseline "
                      f"that this run can't prove gone:")
        by_reason: dict[str, list[str]] = {}
        for n, why in doubts.items():
            by_reason.setdefault(why, []).append(n)
        for why, which in by_reason.items():
            console.print(f"  - {escape(', '.join(which))}: {escape(why)}", soft_wrap=True)
        console.print("  if a removal is intended, accept it with `nightward approve <name>`.")
    elif held:
        console.print(f"[yellow]kept[/yellow] {len(held)} REMOVED behavior(s) in the baseline: "
                      f"{escape(', '.join(held))}\n  removals may come from skipped tests or "
                      f"a partial path. Accept them with `nightward approve <name>` or "
                      f"`{'--group ... ' if groups else '--all '}--include-removed`.",
                      soft_wrap=True)
    _print_summary(recompute(store, judge=judge))


@app.command()
@handle_errors
def reject(name: str, dir: str = typer.Option(DEFAULT_DIR)):
    """Confirm a change you reviewed as a real regression. The boundary stays breached.

    Works on any unapproved change and on a judged-SAME ruling (a rejection
    overrules the judge). `approve --all` skips it while the same payload is
    pending; an explicit `approve <name>` overrides and clears the rejection.
    """
    store = _existing_store(dir)
    with store_lock(store.root, "nightward reject"):
        report = _fresh_report(store, f"reject {name!r}")
        listed = {it["name"] for it in _report_items(report)}
        if name not in listed:
            # A slip of the name must not "reject" a correct behavior (R3-FIN-07).
            open_ = sorted(_blast_names(report))
            raise NightwardError(
                f"{name!r} is not an unapproved change (or a judged-SAME ruling) in the "
                f"last report - nothing to reject. "
                + (f"Unapproved: {_shown(open_, 10)}" if open_ else "Nothing is unapproved."))
        baseline, pending = store.load_baseline(), store.load_pending()
        # Reject the capture the human saw, not whatever is pending now (R3-FIN-02).
        _check_reviewed(store, [name], baseline, pending, "reject")
        store.mark_rejected(name, by=_who())
        report = recompute(store, judge=judge_from_meta(store), baseline=baseline,
                           pending=pending)
    console.print(f"[red]rejected[/red] {escape(name)} - the boundary is "
                  f"{escape(report['boundary'])} ({report['unapproved']} unapproved) until the "
                  f"code changes. Fix it and re-run `nightward run`.", soft_wrap=True)
    ignored = [p for p in _ignore_problems(dir) if p.startswith("rejections")]
    if ignored:
        err_console.print(f"[yellow]warning:[/yellow] {escape(ignored[0])}", soft_wrap=True)
    else:
        console.print(f"[dim]commit {escape(store.rejected_dir.as_posix())}/ with your "
                      f"change: a rejection protects every clone that has it[/dim]",
                      soft_wrap=True)


# doctor's marks: ~ noise with a remedy, * looks real, ! shape changed.
# Dates and reorders look real first: they can be the contract (D15).
_DOCTOR_MARKS = {"volatile": "~", "float-noise": "~", "order-only": "*", "date": "*",
                 "content-hash": "*", "changed": "*", "structural": "!", "json-format": "~"}
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
    sure = [s for s in diag["suggestions"] if not s["conditional"]]
    maybe = [s for s in diag["suggestions"] if s["conditional"]]
    for title, rules in (
            ("volatile by evidence - if this is noise, tame it in the conftest.py "
             "named on each rule:", sure),
            ("only if these dates are not part of the contract - re-run with no code "
             "edits first; if they change again, tame them in the conftest.py named on "
             "each rule:", maybe)):
        if not rules:
            continue
        console.print(f"\n[bold]{escape(title)}[/bold]", soft_wrap=True)
        console.print("  [cyan]from nightward import scrub[/cyan]")
        for s in rules:
            console.print(f"  [cyan]{escape(s['rule'])}[/cyan]  [dim]# in "
                          f"{escape(s['conftest'])}: {escape(s['reason'])}[/dim]",
                          soft_wrap=True)
    if diag["suggestions"]:
        console.print("then re-run [cyan]nightward run[/cyan] and review what is left. "
                      "Every rule above is scoped to the field or text that drifted and "
                      "matches no stable value elsewhere in this capture.", soft_wrap=True)
        if any("register_field" in s["rule"] for s in diag["suggestions"]):
            console.print("[yellow]caution:[/yellow] register_field masks that key in "
                          "[bold]every[/bold] behavior captured under that conftest.py's "
                          "directory, including ones you add later.", soft_wrap=True)
    if looks_real:
        console.print("\n[bold]*[/bold] / [bold]![/bold] look like real changes: "
                      "`nightward review`, then approve or fix - never scrub a regression. "
                      "doctor calls a value volatile only when the value shows it. If one of "
                      "these changes again on a re-run with no code edits, it is volatile: "
                      "tame it at capture time in that test as its note says (sort, round, "
                      "normalize), or mask it.", soft_wrap=True)


@app.command()
@handle_errors
def gate(dir: str = typer.Option(DEFAULT_DIR)):
    """Exit with the verdict of the last run (for CI / agent loops).

    0: boundary intact. 1: breached, stale (baseline or capture changed since
    the report) or incomplete (capture tests failed). 2: no store, no report
    (no run yet, or the last run aborted) or another error. Only 0 is a pass.
    """
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
        _print_not_run(report)
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
    _warn_unless_ignored(out_path / "data.json", "it holds your captured behaviors",
                         _ignore_fix(out_path))
    if serve:
        from .view.serve import serve as _serve
        _serve(out_path, port=port, open_browser=open_browser)
    else:
        # Loopback only, like --serve: data.json holds captured behaviors, and
        # http.server alone listens on every interface (R3-WEB-05).
        shell = shellquote.default_shell()
        site = shellquote.quote(str(out_path), shell) or str(out_path)
        store_arg = ("" if dir == DEFAULT_DIR
                     else f" --dir {shellquote.quote(dir, shell) or dir}")
        console.print(f"open it with:  [cyan]nightward view{escape(store_arg)} --out "
                      f"{escape(site)} --port {port}"
                      f"[/cyan]\n  or:  [cyan]python -m http.server --bind 127.0.0.1 -d "
                      f"{escape(site)} {port}[/cyan]  (fetch needs http, not file://)",
                      soft_wrap=True)


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
    if not generated_at:   # a report from an older nightward (R3-OPS-05)
        return "(no run time recorded - re-run `nightward run`)"
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
                      f"[dim]({escape(ch.get('group') or '(ungrouped)')})[/dim]"
                      f"{_rejected_note(ch)}")
    _print_not_run(payload)
    if payload.get("judged_same"):
        same = [it["name"] for it in payload["judged_same"]]
        console.print(f"[dim]{len(same)} change(s) ruled semantically SAME by the judge: "
                      f"{escape(_names_text(same))} - audit with `nightward review`[/dim]",
                      soft_wrap=True)
    console.print(f"[dim]{escape(_as_of(payload.get('generated_at')))}[/dim]")


@app.command("mcp")
@handle_errors
def mcp_cmd(judge: str | None = typer.Option(
        None, help="Semantic judge for nightward_run, as provider:model. The agent "
                   "can't choose it. Default: [tool.nightward] judge in the pyproject.toml "
                   "nearest the store"),
            dir: str = typer.Option(
        DEFAULT_DIR, help="The store the agent's tools gate. The agent can't choose "
                          "another one")):
    """Start the MCP server (stdio) for AI agents - exposes run/status, NOT approve."""
    from .mcp_server import serve
    serve(judge=judge, dir=dir)


if __name__ == "__main__":
    app()
