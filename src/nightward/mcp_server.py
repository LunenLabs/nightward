"""MCP server — let an AI agent trigger the gate, but never approve it.

Exposes run/status (read·execute). approve/reject live only in the human CLI:
trigger != approval, or the gate is dead (spec §2). The tool functions below do
NOT import mcp, so the gate logic stays testable without the optional dependency.
"""
from __future__ import annotations

from pathlib import Path

from .config import project_judge
from .core.baseline import Store
from .errors import NightwardError
from .judge import parse_spec
from .runner import execute_run, is_stale
from .signal import status_payload

# The semantic judge for nightward_run. Set by the human who configures the
# server (`nightward mcp --judge SPEC`), never by the agent: an agent that could
# pick its own judge could pick persona:lenient and wave its changes through.
_server_judge: str | None = None
# The store the server gates, resolved when the human starts it (`nightward mcp
# --dir`, default .nightward). The agent's `dir` may only name this store: a
# copy with a forged baseline in an ignored folder would otherwise gate green
# with nothing in `git status` (R3-LLM-07). None = not started as a server
# (library use): any store, still named in every result.
_server_dir: Path | None = None


def configure(judge: str | None = None, dir: str | None = None) -> None:
    """Set the server's judge spec and store; a bad spec fails at startup, not mid-loop."""
    global _server_judge, _server_dir
    if judge:
        parse_spec(judge)
    _server_judge = judge
    _server_dir = Path(dir).resolve() if dir else None


def _store_dir(dir: str | None) -> str:
    """The store a tool call gates: the server's, which `dir` may only name."""
    if _server_dir is None:
        return dir or ".nightward"
    if dir and Path(dir).resolve() != _server_dir:
        raise NightwardError(
            f"this server gates the store {_server_dir}; dir={dir!r} names another one. "
            f"Omit dir - a human chooses the store when starting the server "
            f"(`nightward mcp --dir PATH`)")
    return str(_server_dir)


def _judge_spec(dir: str) -> str | None:
    # The server option, else the committed [tool.nightward] judge of the
    # project that owns the store (D14, D22) - never of the agent's `path`. Never
    # $NIGHTWARD_JUDGE or the last run's judge: a human's one-off
    # `nightward run --judge persona:lenient` must not become the agent's gate.
    return _server_judge or project_judge(dir)


def run_tool(path: str = ".", dir: str | None = None, timeout: int = 600) -> dict:
    """Run the tests, capture behaviors, and return the boundary signal.

    Call this after every code edit: nightward_status only reports the last run.
    path: what pytest runs (relative to the server's working directory);
    dir: omit it - the server gates the store a human started it with
    (`nightward mcp --dir`), and any other store is refused; timeout: seconds
    before pytest is stopped (the store is then left untouched and the last
    report invalidated).
    Returns {boundary, unapproved, changes: [{name, kind, group, judged,
    judge_model, judge_reason, judge_replayed}], judged_same, stale, incomplete,
    generated_at, judge, store (absolute path of the gated store), path (what
    pytest ran), warnings: {skipped, failed, errors, deselected, xfailed, scrubbed,
    scrub_unmatched (custom scrub rules that matched nothing),
    pytest_returncode, pytest_output_tail}}.
    boundary is one of:
      "intact"     done: no unapproved change;
      "breached"   unapproved changes: fix the code, or ask a human to approve;
      "incomplete" nothing unapproved, but capture tests failed or errored
                   (see incomplete and pytest_output_tail): fix the tests;
      "stale"      the baseline or capture moved since the report: run again;
      "unknown"    no report yet: run.
    Done means boundary == "intact" and stale is false. Behaviors approved with
    semantic=True are judged by the judge the humans committed
    ([tool.nightward] judge in pyproject.toml, or `nightward mcp --judge`);
    "judge" says which, and why it was unavailable if it was. This tool cannot
    approve changes: a human does that with the nightward CLI.
    """
    dir = _store_dir(dir)
    result = execute_run(path, dir, capture_output=True, timeout=timeout,
                         judge_spec=_judge_spec(dir), command="nightward_run (MCP)")
    payload = status_payload(result["report"])
    payload["store"] = str(Path(dir).resolve())
    payload["path"] = path
    payload["warnings"] = {
        "skipped": result["skipped"],
        "failed": result["failed"],
        "errors": result["errors"],
        "deselected": result["deselected"],
        "xfailed": result["xfailed"],
        "scrubbed": result["scrubbed"],
        "scrub_unmatched": result["scrub_unmatched"],
        "pytest_returncode": result["pytest_returncode"],
        "pytest_output_tail": result["output_tail"],
    }
    return payload


def status_tool(dir: str | None = None) -> dict:
    """Read the boundary signal of the LAST nightward_run, without running tests.

    It does not see code edits made since that run: after changing code, call
    nightward_run for a fresh verdict. Same shape as nightward_run, without
    warnings. boundary is "intact" (done), "breached" (unapproved changes),
    "incomplete" (capture tests failed or errored; see incomplete), "stale"
    (the baseline or capture moved since that run, so its verdict can't be
    trusted: run again) or "unknown" (no report yet). generated_at says when
    the run happened; store names the store read (dir: omit it, as for
    nightward_run).
    """
    store = Store(Path(_store_dir(dir)))
    report = store.load_report()
    payload = status_payload(report, stale=is_stale(store, report))
    payload["store"] = str(store.root.resolve())
    return payload


# The agent-facing surface. approve / reject / init / view are intentionally ABSENT.
_TOOLS = {
    "nightward_run": run_tool,
    "nightward_status": status_tool,
}


def build_server():
    """Build the FastMCP server with only the read/execute tools registered."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        # mcp 2.x renamed FastMCP, so "installed but wrong major" lands here too.
        raise NightwardError(
            "MCP support needs the mcp 1.x SDK - run: pip install 'nightward[mcp]' "
            "(mcp 2.x is not supported yet)"
        ) from exc
    server = FastMCP("nightward")
    for name, fn in _TOOLS.items():
        server.tool(name=name)(fn)
    return server


def serve(judge: str | None = None, dir: str = ".nightward") -> None:
    """Start the stdio MCP server (blocks). FastMCP defaults to stdio transport."""
    configure(judge=judge, dir=dir)
    build_server().run()
