"""nightward CLI - init / run / review / doctor / approve / reject / gate / status."""
from __future__ import annotations

import functools
import json
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

app = typer.Typer(
    help="nightward - regression firewall for AI-driven changes",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(legacy_windows=False)
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
    if result["pytest_returncode"] == 1:
        err_console.print("[yellow]warning:[/yellow] some tests failed - captured "
                          "behaviors may be incomplete; blast radius may be unreliable")
    if result["skipped"]:
        err_console.print(f"[yellow]warning:[/yellow] {result['skipped']} test(s) skipped - "
                          "skipped behaviors appear as REMOVED; blast radius may show "
                          "false positives")
    _print_summary(result["report"])


@app.command()
@handle_errors
def review(dir: str = typer.Option(DEFAULT_DIR)):
    """Show the blast radius with full diffs."""
    report = _require_report(_store(dir))
    if report.get("boundary") == "intact":
        console.print("[green]boundary intact - nothing to review[/green]")
        return
    for group, items in report.get("blast_radius", {}).items():
        console.print(f"\n[yellow]group: {escape(group)}[/yellow]")
        for it in items:
            console.print(f"\n[bold][[cyan]{it['kind']}[/cyan]] {escape(it['name'])}[/bold]")
            diff = it.get("diff", "")
            console.print(escape(diff) if diff else "[dim](no text diff)[/dim]")


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
                help="With --all, also accept REMOVED behaviors (drops them from the baseline)"),
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
    if all_:
        changes = [c for c in compare(baseline, pending, judge=judge) if c.kind != UNCHANGED]
        removed = [c.name for c in changes if c.kind == REMOVED]
        if removed and include_removed:
            # A skipped/failed test captures nothing and looks REMOVED; approving
            # that would silently shrink the boundary. Demand a complete run.
            meta = store.load_run_meta()
            if meta.get("skipped") or meta.get("failed"):
                raise NightwardError(
                    f"refusing --include-removed: the last run had "
                    f"{meta.get('skipped', 0)} skipped and {meta.get('failed', 0)} failed "
                    f"test(s), so REMOVED may be false. Re-run cleanly, or approve "
                    f"removals one by name."
                )
        if not include_removed:
            held = removed
        targets = [c.name for c in changes if c.name not in held]
    elif name:
        targets = [name]
    else:
        raise NightwardError("specify a behavior name or --all")
    if not targets and not held:
        console.print("nothing to approve - boundary already intact")
        return

    for n in targets:
        verb = _approve_one(store, n, baseline, pending)
        console.print(f"[green]{verb}[/green] {escape(n)}")
    if held:
        console.print(f"[yellow]kept[/yellow] {len(held)} REMOVED behavior(s) in the baseline: "
                      f"{escape(', '.join(held))}\n  removals may come from skipped tests or "
                      f"a partial path. Accept them with `nightward approve <name>` or "
                      f"`--all --include-removed`.", soft_wrap=True)
    _print_summary(recompute(store, judge=judge))


@app.command()
@handle_errors
def reject(name: str, dir: str = typer.Option(DEFAULT_DIR)):
    """Confirm a change as a real regression. Boundary stays breached."""
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
def doctor(dir: str = typer.Option(DEFAULT_DIR)):
    """Explain what moved in CHANGED behaviors; suggest scrub rules only for
    values that are volatile by evidence (timestamps, random tokens)."""
    from .core.doctor import diagnose
    store = _store(dir)
    pending = store.load_pending()
    if not pending:
        raise NightwardError("no pending capture - run `nightward run` first")
    diag = diagnose(store.load_baseline(), pending)
    if not diag["changed"]:
        console.print("[green]no CHANGED behaviors - nothing to diagnose[/green]")
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
        console.print("[red]report is stale[/red] - the baseline changed since the last run; "
                      "re-run `nightward run`")
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
        print(json.dumps(payload, ensure_ascii=False))
    else:
        console.print(payload)


@app.command("mcp")
@handle_errors
def mcp_cmd():
    """Start the MCP server (stdio) for AI agents - exposes run/status, NOT approve."""
    from .mcp_server import serve
    serve()


if __name__ == "__main__":
    app()
