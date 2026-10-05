"""MCP server — let an AI agent trigger the gate, but never approve it.

Exposes run/status (read·execute). approve/reject live only in the human CLI:
trigger != approval, or the gate is dead (spec §2). The tool functions below do
NOT import mcp, so the gate logic stays testable without the optional dependency.
"""
from __future__ import annotations

import os
from pathlib import Path

from .core.baseline import Store
from .errors import NightwardError
from .judge import parse_spec
from .runner import execute_run, is_stale
from .signal import status_payload

# The semantic judge for nightward_run. Set by the human who configures the
# server (`nightward mcp --judge SPEC`), never by the agent: an agent that could
# pick its own judge could pick persona:lenient and wave its changes through.
_server_judge: str | None = None


def configure(judge: str | None = None) -> None:
    """Set the server's judge spec; a bad spec fails at startup, not mid-loop."""
    global _server_judge
    if judge:
        parse_spec(judge)
    _server_judge = judge


def _judge_spec(dir: str) -> str | None:
    # Server option > $NIGHTWARD_JUDGE > the judge of the last run (e.g. the
    # team's `nightward run --judge`), so the agent sees the CLI's verdict.
    return (_server_judge or os.environ.get("NIGHTWARD_JUDGE")
            or Store(Path(dir)).load_run_meta().get("judge"))


def run_tool(path: str = ".", dir: str = ".nightward", timeout: int = 600) -> dict:
    """Run the tests, capture behaviors, and return the boundary signal.

    Call this after every code edit: nightward_status only reports the last run.
    path: what pytest runs (relative to the server's working directory);
    dir: the nightward store; timeout: seconds before pytest is stopped (the
    store is then left untouched).
    Returns {boundary: intact|breached|unknown, unapproved, changes: [{name,
    kind, group, judged...}], judged_same, stale, generated_at, judge, warnings:
    {skipped, failed, scrub_unmatched (custom scrub rules that matched
    nothing), pytest_returncode, pytest_output_tail}}. Done means
    boundary == "intact" and stale is false. Behaviors captured with
    semantic=True are judged by the judge the human configured (server
    --judge, $NIGHTWARD_JUDGE, or the last run's judge); "judge" says which,
    and why it was unavailable if it was. This tool cannot approve changes:
    a human does that with the nightward CLI.
    """
    result = execute_run(path, dir, capture_output=True, timeout=timeout,
                         judge_spec=_judge_spec(dir))
    payload = status_payload(result["report"])
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


def status_tool(dir: str = ".nightward") -> dict:
    """Read the boundary signal of the LAST nightward_run, without running tests.

    It does not see code edits made since that run: after changing code, call
    nightward_run for a fresh verdict. stale=True means the baseline changed
    since that run, so its verdict can't be trusted either; generated_at says
    when it ran. A missing report gives boundary "unknown".
    """
    store = Store(Path(dir))
    report = store.load_report()
    return status_payload(report, stale=is_stale(store, report))


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


def serve(judge: str | None = None) -> None:
    """Start the stdio MCP server (blocks). FastMCP defaults to stdio transport."""
    configure(judge=judge)
    build_server().run()
