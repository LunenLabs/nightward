"""`gate`, `view`, `status` and `mcp`: read the verdict."""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.markup import escape

from .. import shellquote
from ..errors import NightwardError
from ..runner import is_stale
from ..signal import status_payload
from ..view import build_site
from ._common import (
    DEFAULT_DIR,
    DEFAULT_SITE,
    STALE_MESSAGE,
    _existing_store,
    _incomplete_text,
    _mark_reviewed,
    _missing_store_message,
    _names_text,
    _print_env_mismatch,
    _print_not_run,
    _print_scrub_rules,
    _rejected_note,
    _require_report,
    _stdout,
    _store,
    app,
    console,
    err_console,
    handle_errors,
)
from ._gitignore import _ignore_fix, _warn_unless_ignored
from .init_run import _exit_if_incomplete, _run


def _gate_run(dir: str, path: str | None) -> dict:
    """`gate --run`: the capture `run` makes, with the path and judge it would pick."""
    result = _run(path, dir, None, [])
    console.print()
    return {"report": result["report"], "pytest_returncode": result["pytest_returncode"]}


@app.command()
@handle_errors
def gate(dir: str = typer.Option(DEFAULT_DIR),
         allow_not_run: bool = typer.Option(
             False, "--allow-not-run",
             help="Pass a partial boundary: approved behaviors whose test the run "
                  "deselected (-k/-m) are not checked. Only for a deliberately narrowed "
                  "CI job whose full run happens elsewhere"),
         run_first: bool = typer.Option(
             False, "--run",
             help="Run the tests and capture first (like `nightward run`), then gate "
                  "on that fresh verdict instead of the last run's"),
         path: str | None = typer.Option(
             None, "--path",
             help="With --run: the path passed to pytest. Default: [tool.nightward] path "
                  "in the pyproject.toml nearest the store, else .")):
    """Exit with the verdict of the last run (for CI / agent loops).

    0: boundary intact (or partial, with --allow-not-run). 1: breached, partial
    (behaviors not checked), stale (baseline, capture or rejections changed since the
    report) or incomplete (capture tests failed). 2: no store, no report (no
    run yet, or the last run aborted) or another error. Only 0 is a pass.

    --run captures first (`nightward run`), so the verdict covers the code as it
    is now, not as it was at the last run.
    """
    if path is not None and not run_first:
        raise NightwardError("--path only applies with --run (gate alone reads the last run)")
    if run_first:
        _existing_store(dir)   # gate never creates a store, --run or not
        _exit_if_incomplete(**_gate_run(dir, path))
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
    if report.get("boundary") == "partial":
        # "Not checked" is never "done" unless the job says so (D23).
        n = len(report.get("not_run") or [])
        if allow_not_run:
            console.print(f"[yellow]boundary partial[/yellow] - passed with --allow-not-run "
                          f"({n} not checked){as_of}")
            _print_not_run(report)
            raise typer.Exit(0)
        console.print(f"[red]boundary partial[/red] ({n} behavior(s) not checked){as_of}")
        _print_not_run(report)
        console.print("a run that deselects approved behaviors is not done - run them, or "
                      "pass --allow-not-run in a deliberately narrowed CI job")
        raise typer.Exit(1)
    console.print(f"[red]boundary breached[/red] ({report.get('unapproved', 0)} "
                  f"unapproved){as_of}")
    _print_env_mismatch(report)
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
        from ..view.serve import serve as _serve
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
                  "partial": "yellow",
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
        console.print("the baseline, the capture or a rejection changed since the last report; "
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
    _print_scrub_rules(payload.get("scrub_rules") or [])
    _print_env_mismatch(payload)
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
    from ..mcp_server import serve
    serve(judge=judge, dir=dir)

