"""`review`: show what moved, and mark it reviewed."""
from __future__ import annotations

import typer
from rich.markup import escape

from .. import shellquote
from ..errors import NightwardError
from ..runner import is_stale
from ._common import (
    DEFAULT_DIR,
    STALE_MESSAGE,
    _blast_names,
    _existing_store,
    _incomplete_text,
    _mark_reviewed,
    _print_env_mismatch,
    _print_scrub_rules,
    _rejected_note,
    _require_report,
    _shown,
    app,
    console,
    err_console,
    handle_errors,
)

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
    cmd = escape(shellquote.command('review', [it['name']]) or 'nightward review')
    console.print(escape("\n".join(lines[:max_lines])))
    console.print(f"[dim]... {len(lines) - max_lines:,} more diff line(s) - see all with "
                  f"`{cmd} --max-lines 0`, or what moved where with `{cmd} --summary`[/dim]",
                  soft_wrap=True)


_SUMMARY_PATHS = 30   # per behavior; the rest is counted


def _print_change_summary(it: dict, baseline, pending, max_lines: int) -> None:
    """What moved where, per path (`review --summary`); the diff when nothing
    path-level differs (e.g. a payload a standing rejection holds)."""
    from ..core import summary
    old, new = baseline.get(it["name"]), pending.get(it["name"])
    if old is not None and new is not None:
        order, hidden = summary.order_rows(old.payload, new.payload)
        rows = [(s.path, f"{s.changed}/{s.total} changed" if s.total > 1 else "",
                 summary.change_line(s)) for s in summary.changes(old.payload, new.payload)
                if not summary.is_hidden(s.path, hidden)]
    elif old is not None or new is not None:
        b = new if new is not None else old
        if new is None:
            console.print("[dim]removed; it was:[/dim]")
        order, _ = summary.order_rows(None, b.payload)
        rows = [(path, "", summary.distribution_line(values))
                for path, values in summary.distribution(b.payload)]
    else:
        order, rows = [], []
    # The order first: folding indices into [*] below drops it.
    rows = [(path, "", line) for path, line in order] + rows
    if not rows:
        _print_diff(it, max_lines)
        return
    for path, count, line in rows[:_SUMMARY_PATHS]:
        count = f"  [dim]{escape(count)}[/dim]" if count else ""
        console.print(f"  [cyan]{escape(path)}[/cyan]{count}  {escape(line)}", soft_wrap=True)
    if len(rows) > _SUMMARY_PATHS:
        cmd = escape(shellquote.command("review", [it["name"]]) or "nightward review")
        console.print(f"  [dim]... {len(rows) - _SUMMARY_PATHS} more path(s) - the full "
                      f"diff: `{cmd} --max-lines 0`[/dim]", soft_wrap=True)


@app.command()
@handle_errors
def review(names: list[str] | None = NAMES_ARG,
           group: list[str] | None = GROUP_OPT,
           max_lines: int = typer.Option(60, "--max-lines",
                                         help="Diff lines shown per behavior; 0 = all"),
           summary: bool = typer.Option(
               False, "--summary",
               help="Instead of diffs, list each changed path once with its old -> new "
                    "values counted (list indices folded into [*]; lists of up to 20 "
                    "items also show their order)"),
           dir: str = typer.Option(DEFAULT_DIR)):
    """Show the blast radius with diffs, plus what the judge ruled SAME."""
    store = _existing_store(dir)
    report = _require_report(store)
    if is_stale(store, report):
        # Its diffs compare inputs that are no longer on disk - don't show them.
        console.print(STALE_MESSAGE)
        raise typer.Exit(1)
    if summary:
        # Not stale: these are exactly the payloads the report compared.
        baseline, pending = store.load_baseline(), store.load_pending()

        def show(it: dict) -> None:
            _print_change_summary(it, baseline, pending, max_lines)
    else:
        def show(it: dict) -> None:
            _print_diff(it, max_lines)
    if report.get("incomplete"):
        err_console.print(f"[yellow]warning:[/yellow] capture incomplete: "
                          f"{_incomplete_text(report['incomplete'])}")
    _print_scrub_rules(report.get("scrub_rules") or [])
    _print_env_mismatch(report)
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
            show(it)
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
            show(it)


# A model's ruling replayed from the committed ledger was not made this run:
# whoever last edited the ledger made it (D22).
_REPLAYED = " (replayed from the committed ledger, not ruled this run)"

