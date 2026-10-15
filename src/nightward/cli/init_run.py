"""`init`, `run` and `report`: create the store and capture."""
from __future__ import annotations

import os
import shlex
from pathlib import Path

import typer
from rich.markup import escape

from ..config import judge_setting
from ..errors import NightwardError
from ..runner import execute_run, recompute_capture, refused_run_invalidates
from ..runner import store_above as _store_above
from ._common import (
    DEFAULT_DIR,
    TRANSIENT_ENTRIES,
    _check_dir,
    _existing_store,
    _incomplete_text,
    _legacy_rejection_note,
    _missing_store_message,
    _names_text,
    _print_summary,
    _shown_path,
    _store,
    app,
    console,
    err_console,
    handle_errors,
)
from ._gitignore import (
    _gitignore_lines,
    _ignore_problems,
    _legacy_gitignore_lines,
    _warn_ignore_problems,
)


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
    # Records older versions wrote as audit notes: say so before they get committed.
    _legacy_rejection_note(store.legacy_rejections())
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
                       "(e.g. anthropic:claude-haiku-5-5, persona:editor), for this run "
                       "only. Default: $NIGHTWARD_JUDGE, else [tool.nightward] judge "
                       "in the pyproject.toml nearest the store")):
    """Re-run tests, capture behaviors, compute the blast radius.

    Extra pytest arguments go after `--`: nightward run tests -- -m "not gpu" -p no:randomly
    (without a path, `nightward run -- -k clamp` runs ".").
    """
    extra = [a for a in ctx.args if a != "--"]
    if path.startswith("-"):
        # `nightward run -- -k clamp`: click hands the first pytest arg to PATH,
        # which is optional (R3-OPS-05).
        path, extra = ".", [path, *extra]
    result = _run(path, dir, judge, extra)
    _exit_if_incomplete(result["report"], result["pytest_returncode"])


def _run(path: str, dir: str, judge: str | None, extra: list[str]) -> dict:
    """Capture and print what `run` prints; the caller decides the exit code."""
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
    for rule in result["scrub_rules"]:
        # What a custom rule replaced is part of what the gate saw (D28): say it
        # whenever the count moves, e.g. a rule that starts rewriting a value.
        if not (rule["values"] and rule["changed"]):
            continue
        names = _names_text(rule["behaviors"])
        rewrite = ("" if rule["placeholder"] else
                   " - its replacement is not a <PLACEHOLDER>: it writes a plausible value "
                   "(a rewrite, not a mask), so review it")
        err_console.print(
            f"[{'dim' if rule['placeholder'] else 'yellow'}]note: scrub rule "
            f"{escape(rule['rule'])} replaced {rule['values']} value(s) this run (was "
            f"{rule['was'] if rule['was'] is not None else 'not registered'}) in "
            f"{escape(names)}{escape(rewrite)}[/]", soft_wrap=True)
    for rule in result["scrub_unmatched"]:
        # The user believes this noise is handled; it isn't (R1-WEB-03).
        why = ("no captured dict has that key" if rule.startswith("register_field(") else
               "patterns see the canonical JSON text, where a '\"' inside a string is "
               "written '\\\"' and a newline '\\n' - see `help(nightward.scrub.register)`")
        err_console.print(f"[yellow]warning:[/yellow] scrub rule {escape(rule)} matched "
                          f"nothing in this run ({escape(why)})", soft_wrap=True)
    _print_summary(result["report"])
    added = result.get("judge_added") or []
    if added:
        # API rulings replay from the committed ledger (D22): an uncommitted
        # one makes CI and teammates gate differently (R5-POL-02).
        ledger = (Path(dir) / "judge").as_posix()
        console.print(f"note: this run added {len(added)} judge ruling(s) to {escape(ledger)}/ "
                      f"({escape(_names_text(added, 3))}) - commit {escape(ledger)}/ with the "
                      f"baseline so CI and teammates gate on the same rulings", soft_wrap=True)
    _legacy_rejection_note(result.get("legacy_rejections") or [])
    # run lists names, not diffs, so it marks nothing reviewed (D26).
    if result["report"].get("blast_radius"):
        console.print("[dim]next: `nightward review` shows the diffs; approve what it "
                      "shows[/dim]")
    # A committed report.json lets a CI `gate` pass on an old verdict; an
    # ignored baseline or rejected/ never reaches CI or teammates.
    _warn_ignore_problems(dir)
    return result


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
    _exit_if_incomplete(report)

