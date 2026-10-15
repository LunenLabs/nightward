"""`doctor`: explain what moved and suggest scrub rules only on evidence."""
from __future__ import annotations

import typer
from rich.markup import escape

from ..errors import NightwardError
from ._common import DEFAULT_DIR, _existing_store, app, console, handle_errors
from .review import GROUP_OPT, NAMES_ARG, _scope

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
    from ..core.doctor import diagnose
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

