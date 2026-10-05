"""nightward CLI - init / run / review / doctor / approve / reject / gate / status."""
from __future__ import annotations

import errno
import functools
import json
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from .core.baseline import Store
from .core.diff import REMOVED, UNCHANGED, compare
from .errors import NightwardError
from .runner import execute_run, is_stale, judge_from_meta, recompute
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

# Store entries that are per-run state, relative to the store dir.
# judge_verdicts.json is deliberately NOT here: it is the committed ledger that
# keeps judged-SAME boundaries deterministic on fresh clones / CI.
TRANSIENT_ENTRIES = ("pending/", "rejected/", "report.json", "run_meta.json",
                     "pending.tmp/", "**/*.tmp")
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


def _require_report(store: Store) -> dict:
    report = store.load_report()
    if report is None:
        raise NightwardError("no report yet - run `nightward run` first")
    return report


def _incomplete_text(incomplete: dict) -> str:
    return (f"{incomplete.get('failed', 0)} failed, {incomplete.get('errors', 0)} error(s) "
            f"in the capture run - fix them and re-run `nightward run`")


STALE_MESSAGE = ("[red]report is stale[/red] - the baseline or the capture changed since "
                 "the last report; re-run `nightward run`")


def _gitignore_lines(dir_: str) -> list[str] | None:
    """Ignore rules for the store's transient entries, or None when the store
    lives outside the current directory (a .gitignore here can't name it)."""
    p = Path(dir_)
    if p.is_absolute():
        try:
            p = p.relative_to(Path.cwd())
        except ValueError:
            return None
    prefix = p.as_posix()
    if prefix == ".." or prefix.startswith("../"):
        return None
    return [GITIGNORE_HEADER, *(f"{prefix}/{entry}" for entry in TRANSIENT_ENTRIES)]


def _print_summary(report: dict) -> None:
    c = report["counts"]
    if report["boundary"] == "intact":
        console.print("\n[bold]Boundary:[/bold] [green]intact[/green]")
    else:
        console.print(f"\n[bold]Boundary:[/bold] [red]breached[/red] "
                      f"({report['unapproved']} unapproved)")
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
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        with gi.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("\n".join(missing) + "\n")
        console.print(f"[green]updated[/green] .gitignore (+{len(missing)} lines)")
    console.print("\nNext: capture behaviors with the `behavior` pytest fixture, "
                  "then `nightward run <path>` and `nightward approve --all`.")


@app.command()
@handle_errors
def run(path: str = typer.Argument(".", help="Path passed to pytest"),
        dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir"),
        judge: str | None = typer.Option(
            None, help="Semantic judge for semantic=True behaviors, as provider:model "
                       "(e.g. anthropic:claude-haiku-4-5, persona:editor). "
                       "Default: $NIGHTWARD_JUDGE")):
    """Re-run tests, capture behaviors, compute the blast radius."""
    _check_dir(dir)
    console.print(f"[dim]$ pytest {escape(path)} --nightward-record "
                  f"--nightward-dir {escape(dir)}[/dim]")
    result = execute_run(path, dir, judge_spec=judge)
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
    _print_summary(result["report"])
    incomplete = result["report"].get("incomplete")
    if incomplete or result["pytest_returncode"] == 1:
        # A failing capture test means behaviors are missing from the blast
        # radius; a green exit here would let CI merge it (`gate` fails too).
        detail = (_incomplete_text(incomplete) if incomplete
                  else "pytest reported failures - fix them and re-run `nightward run`")
        err_console.print(f"\n[red]capture incomplete:[/red] {detail}")
        raise typer.Exit(1)


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
    store = _store(dir)
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


def _removal_doubt(b, meta: dict) -> str | None:
    """Why a REMOVED behavior may be false, or None when the run proves it gone.

    Proof: its source test ran to completion this run and did not capture it.
    Baselines recorded before sources existed fall back to a fully clean run.
    """
    if b.source is not None:
        if b.source in meta.get("completed", ()):
            return None
        return (f"its test {b.source} did not run to completion this run (skipped, "
                f"failed, deselected, xfailed, deleted, or outside the run path)")
    counts = [f"{meta[k]} {k}" for k in _NOT_RUN if meta.get(k)]
    if counts:
        return (f"no recorded source test, and the last run was partial "
                f"({', '.join(counts)})")
    return None


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
                help="With --all, also accept REMOVED behaviors whose test ran to completion "
                     "this run without capturing them (drops them from the baseline)"),
            dir: str = typer.Option(DEFAULT_DIR)):
    """Promote pending behavior(s) into the approved baseline."""
    if all_ and name:
        raise NightwardError("give a behavior name or --all, not both")
    store = _store(dir)
    baseline = store.load_baseline()
    pending = store.load_pending()
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
            doubts = {n: why for n in removed if (why := _removal_doubt(baseline[n], meta))}
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
    if not targets and not held and not kept_rejected:
        console.print("nothing to approve - boundary already intact")
        return

    for n in targets:
        verb = _approve_one(store, n, baseline, pending)
        console.print(f"[green]{verb}[/green] {escape(n)}")
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
    store = _store(dir)
    store.mark_rejected(name)
    console.print(f"[red]rejected[/red] {escape(name)} - boundary stays breached. "
                  f"Fix the code and re-run `nightward run`.")


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
    store = _store(dir)
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
    store = _store(dir)
    report = _require_report(store)
    if is_stale(store, report):
        console.print(STALE_MESSAGE)
        raise typer.Exit(1)
    if report.get("incomplete"):
        console.print(f"[red]last run incomplete:[/red] {_incomplete_text(report['incomplete'])}")
        raise typer.Exit(1)
    if report.get("boundary") == "intact":
        console.print("[green]boundary intact[/green]")
        raise typer.Exit(0)
    console.print(f"[red]boundary breached[/red] ({report.get('unapproved', 0)} unapproved)")
    raise typer.Exit(1)


@app.command()
@handle_errors
def view(dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir to read"),
         out: str = typer.Option("nightward-site", help="Output directory for the static site"),
         serve: bool = typer.Option(True, "--serve/--no-serve",
                                    help="Serve locally and open a browser after building"),
         port: int = typer.Option(8000, help="Port for --serve"),
         open_browser: bool = typer.Option(True, "--open/--no-open",
                                           help="Open a browser when serving")):
    """Build a static, read-only blast-radius dashboard (view it in a browser)."""
    _check_dir(dir)
    out_path = build_site(Path(dir), Path(out))
    console.print(f"[green]built[/green] {escape(str(out_path))}/ "
                  "(index.html, app.js, style.css, data.json)")
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
    report = store.load_report()
    payload = status_payload(report, stale=is_stale(store, report))
    if json_:
        print(json.dumps(payload, ensure_ascii=False), file=_stdout)
    else:
        console.print(payload)


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
