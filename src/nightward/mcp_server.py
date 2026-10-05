"""MCP server — let an AI agent trigger the gate, but never approve it.

Exposes run/status (read·execute). approve/reject live only in the human CLI:
trigger != approval, or the gate is dead (spec §2). The tool functions below do
NOT import mcp, so the gate logic stays testable without the optional dependency.
"""
from __future__ import annotations

from pathlib import Path

from .core.baseline import Store
from .errors import NightwardError
from .runner import execute_run, is_stale
from .signal import status_payload


def run_tool(path: str = ".", dir: str = ".nightward", timeout: int = 600) -> dict:
    """Capture behaviors, recompute the blast radius, return the boundary signal.

    timeout (seconds) bounds the pytest run so a hung suite can't hang the server.
    """
    result = execute_run(path, dir, capture_output=True, timeout=timeout)
    payload = status_payload(result["report"])
    payload["warnings"] = {
        "skipped": result["skipped"],
        "failed": result["failed"],
        "errors": result["errors"],
        "deselected": result["deselected"],
        "xfailed": result["xfailed"],
        "pytest_returncode": result["pytest_returncode"],
        "pytest_output_tail": result["output_tail"],
    }
    return payload


def status_tool(dir: str = ".nightward") -> dict:
    """Read the last boundary status without re-running (report absent -> unknown).

    stale=True: the baseline changed since that run. Code edits are NOT detected -
    after changing code, call nightward_run for a fresh verdict.
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


def serve() -> None:
    """Start the stdio MCP server (blocks). FastMCP defaults to stdio transport."""
    build_server().run()
