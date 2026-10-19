# Dogfood fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the three problems the PetTracker dogfood (2026-10-19/20, a FastAPI
backend with async tests and a suite over 15 minutes) hit while using nightward
for real.

**Order:** B1 → B2 → B3. B1 blocks every async project, so it goes first. Each
task is one commit; together they are one PR. Releasing 0.3.4 afterwards is the
maintainer's call.

**Gate before push:** `pytest -q -n auto` and `ruff check .`.

---

## B1. `@capture` on async tests (bug + missing support)

**Observed.** With pytest-asyncio 1.3 (`asyncio_mode = auto`), an
`@capture` async test fails with
`test_x returned None: an @capture test returns the value to capture (did you forget `return`?)`.
The message is wrong: the test did return a value. The existing "can't capture an
async test" guard never fires, because pytest-asyncio has already replaced
`item.obj` with a sync wrapper (`wrap_in_sync`) by the time `pytest_pyfunc_call`
runs. That wrapper discards the coroutine's return value.

**Approach (spike verified on pytest-asyncio 1.3, auto mode).**
Wrap at collection time, before any async plugin wraps the test. The wrapper stays
a coroutine function, so async plugins still recognise and run it.

**Files:** `src/nightward/pytest_plugin.py`, `tests/core/test_capture_decorator.py`,
`pyproject.toml` (dev extra: `pytest-asyncio`, `anyio`), `docs/` (the
`@capture` section).

- [ ] **Failing tests first** (pytester), each asserting the captured payload:
  - auto mode: `async def` + `@capture` returns a dict
  - strict mode: `@pytest.mark.asyncio` + `@capture`
  - anyio: `@pytest.mark.anyio` + `@capture`
  - parametrized async keeps its id (`tax[low]`)
  - async returning `None` gets the existing "returned None" error
  - async generator: still refused, with a clear message
  - no async plugin installed: pytest's own "async def functions are not natively
    supported" outcome, with no nightward traceback
- [ ] In `pytest_collection_modifyitems` (`tryfirst=True`), for each item with the
  `nightward_capture` marker whose `obj` is a coroutine function, replace
  `item.obj` with a `functools.wraps` async wrapper. The wrapper awaits the
  original and stores the result in `item.stash[_RETURNED]`. Build it in a
  factory function, because a closure over the loop variable captures the wrong
  item (this happened in the spike).
- [ ] In `pytest_pyfunc_call`, sync tests keep today's `keep_return` path. For
  stashed async items, don't rewrap: read `item.stash[_RETURNED]` after `yield`,
  then share the "None" check, the naming and `rec.add` with the sync path (one
  helper).
- [ ] `rec.begin(nodeid)` for tests without the `behavior` fixture: confirm it
  still runs before the coroutine body (with a test that also calls `behavior`).
- [ ] Remove the now-dead "can't capture an async test" message, but keep the
  async-generator refusal.
- [ ] Docs: `@capture` works on async tests (pytest-asyncio, anyio).

## B2. A committed default test path (`gate --run`, `run`, MCP)

**Observed.** `run`, `gate --run` and MCP `nightward_run` all default to `"."`.
`gate --run` can't take a path at all. On a suite that takes over 15 minutes, the
agent loop (the gate-loop skill runs `nightward run .`) and `gate --run` can't be
used, even though the captures live in one 3-second directory
(`tests/nightward`). The GitHub Action already has a `path` input; the other
three surfaces have nothing.

**Approach.** The capture suite is a project decision, so it goes in the committed
config next to `judge`:

```toml
[tool.nightward]
path = "tests/nightward"        # or a list of paths
```

- An explicit argument wins (`run PATH`, `gate --run --path PATH`, MCP
  `path=`), then `[tool.nightward] path`, then `"."`.
- Paths in the config resolve against the pyproject.toml's directory, not the
  cwd. A config that means different things from different directories is the
  same trap D22 closed for the judge.
- No new semantics: a narrower explicit path still yields `partial`/REMOVED
  exactly as today.

**Files:** `src/nightward/config.py`, `src/nightward/cli/init_run.py`,
`src/nightward/cli/gate.py`, `src/nightward/mcp_server.py`, `action.yml`
(`path` default: empty, meaning use the config), `plugins/nightward/skills/gate-loop/SKILL.md`
(`nightward run` without `.`), README Commands, `docs/mcp.md`.

- [ ] Failing tests: config string, list, and a wrong type (`NightwardError`
  with the file path); `run` with no argument uses the config; `gate --run`
  uses it; `gate --run --path X` overrides it; MCP `run_tool()` uses it; paths
  resolve from the pyproject dir when the cwd is a subdirectory; no config
  gives `"."` (unchanged).
- [ ] `config.capture_paths(store_dir) -> list[str] | None`, read from the
  pyproject.toml nearest the store (same lookup as `judge`).
- [ ] Wire it through `_run`, `_gate_run` (new `--path` option, repeatable),
  `run_tool` (`path: str | None = None`), and `execute_run` (accepts a list).
- [ ] `action.yml`: empty `path` means "let nightward decide"; keep `"."` for
  users with no config (test it in the action job).
- [ ] Update the skill and docs.

## B3. `review --summary` hides list order

**Observed.** `summary.py` folds list indices into `[*]` and counts values per
path. A ranking (walker search: which walker is listed first) can't be reviewed:

- for a NEW behavior, `default_3km[*].name` shows a set of names, not an order;
- a pure reorder in a CHANGED behavior shows up as "3/4 changed" field values,
  not as "the order changed".

**Approach (display only; fingerprints, verdicts and the store are untouched,
as the module docstring requires).** For each list of 20 items or fewer:

- **Label** per item: the item itself if it is a scalar; for a dict, its first
  string value in sorted-key order (the same order `canonical_json` writes); else
  `#i`.
- **NEW/REMOVED:** add a row `path  order: A, B, C` next to the distributions.
- **CHANGED, same items in a new order** (equal canonical items as a multiset):
  print one row `path  reordered: A, B, C -> A, C, B` and suppress that list's
  per-field rows, which only repeat the reorder.
- **CHANGED, different items:** keep the field rows. Add an `order:` old -> new
  row when the label sequence differs.

**Files:** `src/nightward/core/summary.py`, `src/nightward/cli/review.py`
(`--summary` help text), tests next to `tests/cli/test_review_summary_env.py`.

- [ ] Failing tests:
  - a NEW ranking shows `order:`
  - a pure swap shows `reordered:` and no `[*]` field rows
  - a changed value plus a reorder shows both
  - a list of 21+ items is unchanged from today
  - a list of dicts with no string field uses `#i`
  - a list of scalars labels by value
- [ ] Implement in `summary.py` (a new `order_rows(old, new)`), and render it in
  `_print_change_summary` before the path rows.
- [ ] Re-run on the dogfood payload (`pt.walker_search`) and paste the output
  into the PR description.

---

## Verification

- `pytest -q -n auto` and `ruff check .` green.
- Re-run the dogfood in the SafeWay backend (`nightward run`, no argument, with
  `[tool.nightward] path` set) and convert one capture test to `@capture`. Same
  payloads as before: the captures must not change by switching style.
