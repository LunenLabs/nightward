# nightward — MCP agent gate: a regression firewall the AI pulls itself

**Date:** 2026-06-07
**Status:** Derived from brainstorming → awaiting user review.
**Brainstorming decisions (both chosen by the user):**
- Core user = **the AI agent triggers it itself** (→ MCP is the surface).
- Human's role = **explicit approval / the AI stops and reports when breached** (semi-automatic checkpoint).

**Premise:** README/CLAUDE.md v0.2. This design is the natural **outlet** of the `gate → loop-signal pipeline` in the CLAUDE.md scope guardrails (explicitly *in scope*) — not a new direction, but where the core was already heading.

---

## 1. Goal

Expose nightward as an MCP server that **AI coding agents call directly as a tool**. Right after its own change, the agent calls `nightward_run` → receives the blast radius / boundary signal → proceeds if intact, and **stops and reports to a human** if breached. The heart of the gate, `approve`/`reject` (= the decisions that move the boundary), stays **in the human CLI only**.

In one line: **"Give the AI only a mirror showing *what silently changed*; the stamp saying *this is fine* belongs to the human."**

## 2. Core design principles — isolation = gate survival

| Principle | Content | Rationale |
|------|------|------|
| **Trigger ≠ approval** | The AI gets run/status (read · execute) only. approve/reject (moving the boundary) is human-CLI only. | If the triggering party and the approving party become the same, the gate commits suicide — it degrades into a "change log". **Non-negotiable.** |
| Mirror only, stamp is human | MCP only gives *where* things shook (group/behavior coordinates). Judgment and approval are human. | Same principle as the `view` security model ("read-only, approve only in the CLI"), extended. |
| breached = stop signal | A breached boundary is the stop-condition of the agent loop and the trigger for reporting to a human. | `signal.py` was already designed as a "stop-condition oracle for agent loops". |
| Minimal exposure of captured data | MCP gives coordinates only, not diffs (captured *content*). Humans get the details via CLI `review`. | Shrinks the sensitive-data exposure surface. |

If these four rows break, the whole spec is meaningless. In particular, if row 1 collapses, it is no longer nightward.

## 3. Architecture — thin adapter + shared logic extraction

The MCP server creates almost no new logic. It is a **thin adapter** that calls the existing core. One refactoring comes first.

**Extract shared run logic.** Today `cli.run` tangles pytest subprocess execution + returncode handling + `_recompute` + warning-meta collection + rich console output into one function. Split out the *pure logic*:

```
nightward/runner.py (new):
    execute_run(path, dir) -> RunResult
        # pytest subprocess (-B -m pytest <path> --nightward-record --nightward-dir <dir> -q)
        # returncode handling (5/2 → NightwardError, 1 → failed warning, 0 → normal)
        # _recompute(store) → report
        # collect run_meta (skipped/failed)
        # return {report, skipped, failed, pytest_returncode}   ← no console output

Consumers (both the same truth, different surfaces):
    cli.run            → calls execute_run(), then prints nicely via rich console (existing behavior preserved)
    mcp.nightward_run  → calls execute_run(), then returns status_payload + warnings as JSON
```

→ CLI behavior is unchanged (zero regressions, existing `tests/` preserved); MCP just exports the same measurement through a different surface.

### 3.1 MCP tool surface (stdio server)

| Tool | Exposed | Returns | Side effects |
|------|------|------|----------|
| `nightward_run(path=".", dir=".nightward")` | ✅ | `{boundary, unapproved, changes[], warnings:{skipped, failed, pytest_returncode}}` | Re-runs pytest → updates report.json (only *measures* the boundary, never moves it) |
| `nightward_status(dir=".nightward")` | ✅ | `{boundary, unapproved, changes[]}` (last report) | None (read-only) |
| `approve` / `reject` / `init` / `view` | ❌ **not exposed** | — | Human CLI only |

- `changes[]` = `signal.status_payload`'s `[{name, kind, group}]` as-is (NEW/CHANGED/REMOVED = unapproved).
- **`nightward_run` reads the boundary; it does not move it.** Updating report.json is "measuring the current state", not "approval". Only `approve` moves the baseline (= the boundary). This distinction is why "giving run to the AI is safe, approve is not".

### 3.2 Data flow (implementing the brainstorming diagram)

```
agent changes code
  → MCP nightward_run
      → execute_run: pytest --nightward-record (subprocess) → capture→compare→blast → report
      → status_payload(report) + warnings
  → branch on boundary:
      intact   → agent continues (gate passed)
      breached → agent stops, reports changes[] to the human
                   ├ intended change → human runs CLI `nightward approve` (not MCP!)
                   └ regression      → tells the agent "revert/fix it" → nightward_run again
```

## 4. Errors · encoding

- Agent/user-caused errors become **structured tool errors + clear messages**, not tracebacks (the CLI's `NightwardError` → exit 2 pattern, converted into an MCP error response). Example: `nightward_run` collects 0 pytest tests (returncode 5) → "no tests under `<path>`". By contrast, `nightward_status` returns `boundary:"unknown"` when there is no report — **not an error**. That is the `status_payload` contract (absence of a measurement is expressed as a *state*; only `gate` halts with an error when the report is missing).
- pytest returncode mapping: `0` = normal, `1` = flagged as `warnings.failed` (not blocking; incomplete-capture warning), `2`·`5` = tool error.
- Response JSON is **UTF-8 / `ensure_ascii=False`** (preserves Hangul behavior names) — same as `status --json`.
- **No stdio pollution:** MCP stdio is the protocol channel. Diagnostic/log output goes **only to stderr** (a single stray line on stdout breaks the protocol). Unrelated to the cp949 console pitfall (does not go through the rich console path).

## 5. Usage scenario (semi-automatic checkpoint)

1. The agent modifies a feature.
2. `nightward_run()` → `{boundary:"breached", changes:[{name:"checkout_total", kind:"CHANGED", group:"billing"}, {name:"points", kind:"CHANGED", group:"loyalty"}]}`.
3. Agent: **stops.** "My change touched 2 behaviors across billing and loyalty — checkout_total and points changed. Was that intended?" → reports to the human.
4. Human decides:
   - Intended → in the terminal, `nightward approve checkout_total points` (or `--all`).
   - Regression → "loyalty must not be touched. Revert it." → the agent fixes the code → back to 2.
5. `nightward_run()` again → `{boundary:"intact"}` → the agent is done.

→ The AI loop stopping at a human checkpoint is *the product, not a bug*. "The moment where what the AI silently broke is pushed in front of a human's eyes" is nightward's value.

## 6. Tests (TDD)

`tests/test_mcp.py` (+ `tests/test_runner.py` for the extracted logic):

- **Isolation guard (core):** the MCP server's tool list **contains** `nightward_run`/`nightward_status` and **does not contain** `approve`/`reject`/`init`/`view`. (Prevents regressions of the trigger ≠ approval boundary — if someone accidentally exposes approve, it goes red.)
- `nightward_run` returns exactly the `status_payload` structure for intact/breached reports.
- `warnings` reflects skipped/failed/returncode.
- `nightward_status` returns the last report with no side effects; with no report, `boundary:"unknown"` (not an error — the `status_payload` contract).
- Hangul behavior names are preserved as UTF-8 in the response JSON (`ensure_ascii=False`).
- pytest collects 0 tests (returncode 5) → structured error.
- **Extraction regression guard:** after splitting out `execute_run`, the existing `nightward run` CLI behavior/output is unchanged (existing tests pass + new `test_runner`).

## 7. Dependencies · execution

- New **optional** dep: the official MCP Python SDK. An `mcp = ["mcp"]` extra under `[project.optional-dependencies]` (version pin locked at implementation time). No effect on the core install (`pip install nightward`) — still pytest/typer/rich.
- Execution: add a `nightward mcp` subcommand to the typer app → starts a **stdio** MCP server. The agent (Claude Code, etc.) launches this command as a subprocess and discovers the tools.
- Transport is **stdio only** (local). HTTP/SSE is OUT.

## 8. Scope (YAGNI)

**IN (MVP):**
- Extract the shared `execute_run` logic into `nightward/runner.py` (zero CLI regressions).
- `nightward mcp` stdio server + 2 tools: `nightward_run` / `nightward_status`.
- Isolation (approve/reject not exposed) + isolation-guard tests.

**OUT (deliberately excluded — including what was filtered out this round):**
- Exposing `approve`/`reject` via MCP ❌ (gate suicide).
- Automatic / policy-based approval engine ❌ (brainstorming option 3 rejected).
- Exposing diffs / captured *content* ❌ (coordinates only; details via human CLI `review`).
- HTTP/SSE transport, multi-workspace, PR comment bot ❌.

## 9. Irreversible-constraint check (CLAUDE.md §irreversible)

- Adding MCP is just a **core code change** — a decision independent of whether the repo is public.
- It does not *publish* captured data — if anything it **reduces** exposure by minimizing the surface to coordinates (no diffs).
- → **No violation** of the irreversible constraints. Consistent with the spirit of §irreversible.
