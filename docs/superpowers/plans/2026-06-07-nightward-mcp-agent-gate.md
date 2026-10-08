# nightward MCP Agent Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an AI agent call `nightward_run`/`nightward_status` directly as MCP tools to measure the regression boundary, while `approve`/`reject` (moving the boundary) stay in the human CLI only.

**Architecture:** (1) Extract `cli.run`'s capture logic into `runner.py:execute_run` so the CLI and MCP share *the same measurement* (zero CLI behavior regressions). (2) The tool functions in `mcp_server.py` are pure functions that do not import the `mcp` package, so they are testable without the optional dep — only `build_server` registers them with FastMCP via a lazy import. (3) A `nightward mcp` subcommand starts the stdio server.

**Tech Stack:** Python 3.10+, pytest (subprocess capture), typer (CLI), MCP Python SDK (FastMCP, optional extra), subprocess.

**Source spec:** `docs/superpowers/specs/2026-06-07-nightward-mcp-agent-gate-design.md`

---

## File Structure

| File | Responsibility | New/Modified |
|------|------|-----------|
| `src/nightward/runner.py` | `execute_run` (pytest capture + recompute, no console output) + `recompute`. Shared by CLI/MCP. | **Create** |
| `src/nightward/cli.py` | Switch `run` to use `execute_run` and `approve` to use `recompute`. Remove `_recompute`. Add the `mcp` subcommand. | Modify |
| `src/nightward/mcp_server.py` | `run_tool`/`status_tool` (pure) + `_TOOLS` (agent surface) + `build_server`/`serve` (lazy FastMCP). approve/reject not registered. | **Create** |
| `pyproject.toml` | Add the `mcp` optional extra; add `mcp` to the `dev` extra (for tests). | Modify |
| `tests/test_runner.py` | `execute_run` capture/error unit tests (extraction regression guard). | **Create** |
| `tests/test_mcp.py` | **Isolation guard** (approve not exposed) + run/status behavior + no stdout pollution (capfd) + Hangul. | **Create** |

**Boundary principle (spec §2, non-negotiable):** if `approve`/`reject` get into `_TOOLS`, the gate commits suicide. `build_server` registers only `_TOOLS`, so checking `_TOOLS` = checking the exposed surface — this is the axis of the isolation guard.

---

## Task 1: Extract `runner.py` + refactor `cli.py` (zero CLI regressions)

**Files:**
- Create: `src/nightward/runner.py`
- Test: `tests/test_runner.py`
- Modify: `src/nightward/cli.py` (imports, remove `_recompute`, `run` body, `approve` call site)

- [ ] **Step 1: Write the failing test** — `tests/test_runner.py`

```python
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
```

- [ ] **Step 2: Confirm it fails**

Run: `pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nightward.runner'`

- [ ] **Step 3: Implement `runner.py`**

```python
"""Shared run logic: capture behaviors via pytest, recompute the blast radius.

Used by both `cli.run` (rich console) and the MCP server (JSON) so the two
surfaces report the same measurement. No console output here.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .core.baseline import Store
from .core.blast import aggregate
from .core.diff import compare
from .errors import NightwardError


def recompute(store: Store) -> dict:
    """Compare pending against baseline, aggregate, persist, and return the report."""
    report = aggregate(compare(store.load_baseline(), store.load_pending()))
    store.write_report(report)
    return report


def _pytest_cmd(path: str, dir: str) -> list[str]:
    # -B: no bytecode cache. Rewriting a test file between runs can otherwise
    # re-import a stale .pyc and silently capture OLD behavior (flaky in CI).
    return [sys.executable, "-B", "-m", "pytest", path,
            "--nightward-record", "--nightward-dir", dir, "-q"]


def execute_run(path: str = ".", dir: str = ".nightward", *,
                capture_output: bool = False) -> dict:
    """Run pytest in a subprocess to capture behaviors, then recompute.

    capture_output=True keeps pytest's stdout off this process's stdout — required
    when called from the MCP stdio server (any stray stdout breaks the protocol).
    Returns {report, skipped, failed, pytest_returncode}.
    """
    result = subprocess.run(_pytest_cmd(path, dir), capture_output=capture_output)
    if result.returncode == 5:
        raise NightwardError(f"pytest collected no tests under {path!r}")
    if result.returncode not in (0, 1):
        raise NightwardError(f"pytest exited with code {result.returncode}; aborting")
    store = Store(Path(dir))
    report = recompute(store)
    meta = store.load_run_meta()
    return {
        "report": report,
        "skipped": meta.get("skipped", 0),
        "failed": meta.get("failed", 0),
        "pytest_returncode": result.returncode,
    }
```

- [ ] **Step 4: Confirm the tests pass**

Run: `pytest tests/test_runner.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Refactor `cli.py` to use `runner`**

5a. Replace imports — remove the `from .core.blast import aggregate` line and add `from .runner import execute_run, recompute`. Resulting import block (top of file):

```python
from .core.baseline import Store
from .core.diff import UNCHANGED, compare
from .errors import NightwardError
from .runner import execute_run, recompute
from .signal import status_payload
from .view import build_site
```

5b. **Delete** the `_recompute` helper definition (currently `cli.py:71-74`):

```python
def _recompute(store: Store) -> dict:
    report = aggregate(compare(store.load_baseline(), store.load_pending()))
    store.write_report(report)
    return report
```

5c. **Replace** the `run` command body (currently `cli.py:114-142`) with:

```python
@app.command()
@handle_errors
def run(path: str = typer.Argument(".", help="Path passed to pytest"),
        dir: str = typer.Option(DEFAULT_DIR, help="Nightward storage dir")):
    """Re-run tests, capture behaviors, compute the blast radius."""
    _check_dir(dir)
    console.print(f"[dim]$ pytest {path} --nightward-record --nightward-dir {dir}[/dim]")
    result = execute_run(path, dir)
    if result["pytest_returncode"] == 1:
        err_console.print("[yellow]warning:[/yellow] some tests failed - captured "
                          "behaviors may be incomplete; blast radius may be unreliable")
    if result["skipped"]:
        err_console.print(f"[yellow]warning:[/yellow] {result['skipped']} test(s) skipped - "
                          "skipped behaviors appear as REMOVED; blast radius may show "
                          "false positives")
    _print_summary(result["report"])
```

5d. Switch the last line of the `approve` command (currently `cli.py:195`), `_print_summary(_recompute(store))`, to `recompute`:

```python
    _print_summary(recompute(store))
```

- [ ] **Step 6: Confirm zero CLI regressions with the full suite**

Run: `pytest -q`
Expected: PASS — the existing `tests/test_integration.py` (run/approve/gate/status cycle, no-tests = exit 2, Hangul review) all pass unchanged. `ruff check .` also passes (unused `aggregate` import removed).

- [ ] **Step 7: Commit**

```bash
git add src/nightward/runner.py src/nightward/cli.py tests/test_runner.py
git commit -m "refactor: extract execute_run/recompute into runner.py (CLI behavior unchanged)"
```

---

## Task 2: `mcp_server.py` — tool functions + isolation guard + no stdout pollution

**Files:**
- Create: `src/nightward/mcp_server.py`
- Test: `tests/test_mcp.py`

- [ ] **Step 1: Write the failing test** — `tests/test_mcp.py`

```python
"""MCP adapter — the agent-facing surface.

The isolation guard is the load-bearing test: approve/reject MUST NOT be
reachable through MCP, or the gate self-approves and dies (spec §2).
"""
import pytest

from nightward import mcp_server
from nightward.core.baseline import Store

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
    store.write_report({"boundary": "intact", "unapproved": 0,
                        "counts": {"total": 0, "unchanged": 0, "new": 0,
                                   "changed": 0, "removed": 0},
                        "blast_radius": {}})
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
```

- [ ] **Step 2: Confirm it fails**

Run: `pytest tests/test_mcp.py -v`
Expected: FAIL — `ImportError: cannot import name 'mcp_server'`

- [ ] **Step 3: Implement `mcp_server.py`**

```python
"""MCP server — let an AI agent trigger the gate, but never approve it.

Exposes run/status (read·execute). approve/reject live only in the human CLI:
trigger != approval, or the gate is dead (spec §2). The tool functions below do
NOT import mcp, so the gate logic stays testable without the optional dependency.
"""
from __future__ import annotations

from pathlib import Path

from .core.baseline import Store
from .errors import NightwardError
from .runner import execute_run
from .signal import status_payload


def run_tool(path: str = ".", dir: str = ".nightward") -> dict:
    """Capture behaviors, recompute the blast radius, return the boundary signal."""
    result = execute_run(path, dir, capture_output=True)
    payload = status_payload(result["report"])
    payload["warnings"] = {
        "skipped": result["skipped"],
        "failed": result["failed"],
        "pytest_returncode": result["pytest_returncode"],
    }
    return payload


def status_tool(dir: str = ".nightward") -> dict:
    """Read the last boundary status without re-running (report absent -> unknown)."""
    return status_payload(Store(Path(dir)).load_report())


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
        raise NightwardError(
            "MCP support not installed - run: pip install 'nightward[mcp]'"
        ) from exc
    server = FastMCP("nightward")
    for name, fn in _TOOLS.items():
        server.tool(name=name)(fn)
    return server


def serve() -> None:
    """Start the stdio MCP server (blocks). FastMCP defaults to stdio transport."""
    build_server().run()
```

- [ ] **Step 4: Confirm the tests pass**

Run: `pytest tests/test_mcp.py -v`
Expected: PASS (6 passed) — `build_server`/`serve` are not called yet, so this passes even without `mcp` installed.

- [ ] **Step 5: Commit**

```bash
git add src/nightward/mcp_server.py tests/test_mcp.py
git commit -m "feat(mcp): agent-facing run/status tools with approve/reject isolation"
```

---

## Task 3: `nightward mcp` subcommand + `mcp` optional extra + server build check

**Files:**
- Modify: `pyproject.toml` (`[project.optional-dependencies]`)
- Modify: `src/nightward/cli.py` (`mcp` subcommand)
- Test: `tests/test_mcp.py` (add build_server smoke test)

- [ ] **Step 1: Add the failing tests** — append to the end of `tests/test_mcp.py`

```python
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
```

- [ ] **Step 2: Confirm they fail**

Run: `pytest tests/test_mcp.py::test_build_server_registers_without_error tests/test_mcp.py::test_cli_exposes_mcp_subcommand -v`
Expected: `test_build_server...` SKIP (when not installed) or FAIL (installed but the API doesn't match); `test_cli_exposes_mcp_subcommand` FAIL — no `mcp` in help.

- [ ] **Step 3: Add the dependency to `pyproject.toml`**

Replace the `[project.optional-dependencies]` block (currently the single line `dev = ["pytest>=7", "ruff>=0.5"]`) with:

```toml
[project.optional-dependencies]
dev = ["pytest>=7", "ruff>=0.5", "mcp>=1.0"]
mcp = ["mcp>=1.0"]
```

Then install: `pip install -e ".[dev]"` (brings FastMCP into the dev environment → the smoke test actually runs instead of SKIPping).

- [ ] **Step 4: Add the `mcp` subcommand to `cli.py`**

Add after the `status` command definition, before `if __name__ == "__main__":`:

```python
@app.command("mcp")
@handle_errors
def mcp_cmd():
    """Start the MCP server (stdio) for AI agents - exposes run/status, NOT approve."""
    from .mcp_server import serve
    serve()
```

(When `mcp` is not installed, `build_server` inside `serve()` raises `NightwardError`, and `handle_errors` converts it into exit 2 + a guidance message.)

- [ ] **Step 5: Confirm the tests pass**

Run: `pytest tests/test_mcp.py -v`
Expected: PASS — including `test_build_server_registers_without_error` (mcp is installed in dev), and `test_cli_exposes_mcp_subcommand` passes.

- [ ] **Step 6: Full gate**

Run: `pytest -q` and `ruff check .`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/nightward/cli.py tests/test_mcp.py
git commit -m "feat(mcp): nightward mcp stdio subcommand + mcp optional extra"
```

---

## Self-Review

**1. Spec coverage:**
- spec §3 shared logic extraction (`execute_run`) → Task 1 ✓
- spec §3.1 two tools `nightward_run`/`nightward_status` + return shape (`status_payload` + `warnings`) → Task 2 `run_tool`/`status_tool` ✓
- spec §3.1 approve/reject/init/view not exposed → Task 2 `_TOOLS` + isolation guard test ✓
- spec §4 errors: no-tests → Task 1 `test_execute_run_no_tests_raises` ✓; status with no report → `unknown` → Task 2 `test_status_tool_no_report_is_unknown` ✓
- spec §4 no stdio pollution → `capture_output=True` + Task 2 `test_run_tool_does_not_pollute_stdout` (capfd) ✓
- spec §4 encoding (Hangul) → Task 2 `test_run_tool_preserves_hangul` ✓ (`ensure_ascii=False` on the MCP transport is handled by FastMCP during JSON serialization; the tools are responsible up to returning a dict)
- spec §7 `mcp` optional extra + running `nightward mcp` → Task 3 ✓
- spec §2 "trigger ≠ approval" invariant → isolation guard prevents regressions ✓

**2. Placeholder scan:** no TODO/TBD/"handle appropriately". All code blocks complete. ✓

**3. Type consistency:** Task 2 `run_tool` consumes `execute_run`'s return keys (`report/skipped/failed/pytest_returncode`) as-is. The isolation test checks the `_TOOLS` keys (`nightward_run`/`nightward_status`) as-is. The `recompute` (public) name matches between its `runner.py` definition and the `cli` import/call. ✓

**One unresolved assumption:** FastMCP's `server.tool(name=...)(fn)` registration API and `server.run()`'s default stdio transport are as of MCP SDK 1.x. `test_build_server_registers_without_error` actually verifies this, so a version mismatch shows up as red in Task 3 Step 5 (not hidden).
