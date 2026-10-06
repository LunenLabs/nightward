"""Beta round 2: the MCP tool descriptions (what the agent reads) match what
the tools return."""
from pathlib import Path

from nightward import mcp_server

BOUNDARIES = ("intact", "breached", "incomplete", "stale", "unknown")
SAMPLE = '''
def test_a(behavior):
    behavior("a", {"v": 1}, group="g1")
'''


def test_tool_descriptions_list_every_boundary_value():
    # R2-DATA-05: "incomplete" and "stale" came back but were not documented.
    for tool in (mcp_server.run_tool, mcp_server.status_tool):
        for value in BOUNDARIES:
            assert value in tool.__doc__, (tool.__name__, value)
        assert "incomplete" in tool.__doc__ and "generated_at" in tool.__doc__


def test_run_tool_description_names_every_returned_field(tmp_path):
    (tmp_path / "test_s.py").write_text(SAMPLE, encoding="utf-8")
    out = mcp_server.run_tool(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"))
    doc = mcp_server.run_tool.__doc__
    for key in (*out, *out["warnings"]):
        assert key in doc, key


def test_readme_lists_every_boundary_value():
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    section = readme.split("## AI agents", 1)[1].split("\n## ", 1)[0]
    for value in BOUNDARIES:
        assert f"`{value}`" in section, value
