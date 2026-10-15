"""MCP adapter — the agent-facing surface.

The isolation guard is the load-bearing test: approve/reject MUST NOT be
reachable through MCP, or the gate self-approves and dies (spec §2).
"""
import pytest

from nightward import mcp_server
from nightward.core.baseline import Store
from nightward.runner import recompute

SAMPLE = '''
def test_a(behavior):
    behavior("a", {"v": 1}, group="g1")
'''
KOR = '''
def test_k(behavior):
    behavior("결제", {"v": 1}, group="빌링")
'''


def test_isolation_no_approve_reject_exposed():
    names = set(mcp_server._TOOLS)
    assert names == {"nightward_run", "nightward_status"}
    assert "approve" not in names
    assert "reject" not in names


def test_status_tool_reads_last_report(tmp_path):
    tw = tmp_path / ".nightward"
    store = Store(tw)
    store.ensure()
    # A real report carries digests of its inputs; one without them reads stale.
    recompute(store)
    out = mcp_server.status_tool(str(tw))
    assert out["boundary"] == "intact"


def test_status_tool_no_report_is_unknown(tmp_path):
    out = mcp_server.status_tool(str(tmp_path / "nope"))
    assert out["boundary"] == "unknown"


def test_run_tool_captures_and_signals(tmp_path):
    (tmp_path / "test_s.py").write_text(SAMPLE, encoding="utf-8")
    out = mcp_server.run_tool(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"))
    assert out["boundary"] == "breached"
    assert out["warnings"]["skipped"] == 0
    assert out["warnings"]["pytest_returncode"] == 0


def test_run_tool_does_not_pollute_stdout(tmp_path, capfd):
    (tmp_path / "test_s.py").write_text(SAMPLE, encoding="utf-8")
    mcp_server.run_tool(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"))
    out, _err = capfd.readouterr()
    assert out == ""   # pytest subprocess stdout captured, not leaked to fd 1


def test_run_tool_preserves_hangul(tmp_path):
    (tmp_path / "test_k.py").write_text(KOR, encoding="utf-8")
    out = mcp_server.run_tool(str(tmp_path / "test_k.py"), str(tmp_path / ".nightward"))
    assert "결제" in [c["name"] for c in out["changes"]]


def test_build_server_registers_without_error():
    pytest.importorskip("mcp")
    server = mcp_server.build_server()
    assert server is not None   # builds with FastMCP; tool set fixed by _TOOLS


def test_cli_exposes_mcp_subcommand():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "nightward", "--help"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0
    assert "mcp" in r.stdout
