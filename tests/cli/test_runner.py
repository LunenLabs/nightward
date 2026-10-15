"""runner.execute_run — shared capture logic behind CLI run and the MCP server."""
import pytest

from nightward.errors import NightwardError
from nightward.runner import execute_run

SAMPLE = '''
def test_a(behavior):
    behavior("a", {"v": 1}, group="g1")
'''


def test_execute_run_captures_and_reports(tmp_path):
    (tmp_path / "test_s.py").write_text(SAMPLE, encoding="utf-8")
    result = execute_run(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"))
    assert result["pytest_returncode"] == 0
    assert result["report"]["boundary"] == "breached"   # first capture -> all NEW
    assert result["report"]["counts"]["new"] == 1
    assert result["skipped"] == 0
    assert result["failed"] == 0


def test_execute_run_no_tests_raises(tmp_path):
    (tmp_path / "test_empty.py").write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(NightwardError, match="no tests"):
        execute_run(str(tmp_path / "test_empty.py"), str(tmp_path / ".nightward"))


def test_pytest_child_gets_no_inherited_stdin(tmp_path, monkeypatch):
    # R1-LLM-01: under `nightward mcp` our stdin IS the MCP protocol pipe. On
    # Windows a child inheriting it blocks while the server has a read pending,
    # so nightward_run hung until timeout. pytest never needs stdin.
    import subprocess

    seen = {}
    real_run = subprocess.run

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    (tmp_path / "test_s.py").write_text(SAMPLE, encoding="utf-8")
    execute_run(str(tmp_path / "test_s.py"), str(tmp_path / ".nightward"), capture_output=True)
    assert seen.get("stdin") is subprocess.DEVNULL
