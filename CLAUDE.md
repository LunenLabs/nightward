# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

> **Project**: nightward (PyPI `nightward`) — a **regression firewall** for AI-made changes.

---

## The one concept that decides everything else

nightward is a **gate, not a test generator**. It has no correctness oracle — it
captures what the system *already does*, a human approves that once (baseline),
and every later change is blocked against that snapshot. It never judges whether
a value is "wrong"; it only stops changes from passing **silently**. The human
either approves (intended change) or fixes the code (regression).

→ Practical implication: never hand-write expected values. Capture behavior,
approve via CLI. Any change that introduces false positives kills this tool —
**a noisy gate is a dead gate**.

---

## Commands

```bash
# dev setup (CONTRIBUTING.md)
python -m venv .venv
source .venv/bin/activate        # Windows: .venv/Scripts/activate
pip install -e ".[dev]"

# push gate (both must pass)
pytest -q                        # testpaths=tests (pyproject)
ruff check .                     # line-length 100, py310, select E/F/I/UP/B

# single test
pytest tests/test_view.py
pytest tests/test_view.py::test_build_site_intact
pytest -k timestamp

# dogfooding
nightward run example            # README quickstart fixture (names only)
nightward review                 # shows the diffs - the only CLI step that marks them reviewed (D26)
nightward approve --all          # NEW/CHANGED only; REMOVED needs --remove NAME/--remove-group or --include-removed
nightward approve --group G      # --all limited to group G (any size; the dashboard's group chip)
cd examples/petshop && nightward run .   # cascade demo (baseline committed)
cd examples/newsroom && NEWSROOM_REWRITE=1 nightward run . --judge persona:lenient  # semantic judge demo (key-free)

# MCP surface for AI agents (optional: pip install -e ".[mcp]")
nightward mcp                    # stdio server — exposes nightward_run/nightward_status ONLY
```

Installation registers two entry points (pyproject): the CLI
(`nightward=nightward.cli:app`) and a **pytest11 plugin**
(`nightward=nightward.pytest_plugin`) — the latter makes the `behavior` fixture
available in any test after `pip install -e .`.

---

## Architecture — one data stream

Capture → compare → aggregate → consume, one direction. `src/nightward/`:

```
runner.execute_run  = capture orchestrator (shared by CLI `run` and MCP `nightward_run`): spawns pytest ↓
tests call behavior(name, val, group=, semantic=)   pytest_plugin.py → Recorder
  └ only on sessionfinish with --nightward-record:
        scrub(val)                       scrub.py          normalize volatile fields (below)
        → .nightward/pending/<name>.received.json          baseline.py · Store
        skipped/failed counts → run_meta.json
compare(baseline, pending, judge=)       core/diff.py      fingerprint comparison → list[Change]
  └ kind = NEW / CHANGED / REMOVED / UNCHANGED            (NEW/CHANGED/REMOVED = unapproved)
  └ semantic=True + judge → SAME verdict collapses CHANGED to UNCHANGED (judged=True audit)
aggregate(changes)                       core/blast.py     group buckets + counts → report dict
  └ boundary = "intact" (unapproved 0) | "breached"       → .nightward/report.json
consumers (read report/store):
  status_payload(report)                 signal.py         status --json / gate / MCP
  build_site(dir, out)                   view/__init__.py  static dashboard (data.json)
  run_tool / status_tool                 mcp_server.py     MCP surface (approve/reject NOT exposed)
adapters.from_file/from_pdf/from_docx/from_xlsx/from_text  adapters.py — file formats → stable JSON
```

**Four non-obvious things you must understand:**

| Concept | Key point | Where |
|---|---|---|
| **Two execution contexts** | The plugin runs *inside* pytest. CLI `run` and MCP `nightward_run` share `runner.execute_run`, which spawns pytest *as a subprocess* (`python -m pytest … --nightward-record`) then recomputes. `approve/reject/gate/status/view` and MCP `nightward_status` never run pytest — they only touch the store. | `runner.py`, `cli.py:run`, `mcp_server.py` |
| **fingerprint = equivalence oracle** | `sha256(canonical_json(payload))`. `canonical_json` uses `sort_keys` + `allow_nan=False` — guarantees fingerprint consistency AND human-readable git diffs at once. Capture, store, and scrub all use this one function (the stability linchpin). | `core/behavior.py` |
| **scrub = false-positive defense** | Volatile values (timestamps, uuids) are normalized **before** fingerprinting, or every run shows "changed" and the tool dies. Two stages: ① field-aware `scrub.register_field(name[, repl])` — masks by key name at any depth, JSON-value replacement can't corrupt the payload (**preferred**) ② text regex `scrub.register(pat, repl)` — fallback when no stable key exists (tradeoff: literals that merely *look* like timestamps get replaced too). Rules and `disable_defaults()` called from a conftest.py apply only to tests under its directory (D20: `scrub._caller_conftest` + the fixture passes the test file to `scrub_counted(path=)`); elsewhere they are global. | `scrub.py` |
| **store = git-native golden set** | `baseline/*.approved.json` and the judge **verdict ledger** (`judge/<hash>.json`, one file per ruling so branches merge cleanly; legacy `judge_verdicts.json` is still read) are **committed** (= the boundary + ruling record — model rulings replay deterministically on fresh clones/CI; persona rulings are recomputed every run and their entries are a record only, D22). `rejected/` is committed too (D17: a rejection protects every clone). `pending/`, `report.json`, `run_meta.json`, `reviewed.json`, `.lock` are gitignored (transient). `approve` = copy pending→baseline; `approve_removal` = delete from baseline; `reject` = copy to `rejected/` (boundary stays breached; `approve --all` skips a behavior while its pending state matches the record). | `core/baseline.py` |

---

## The capture idiom (this fixture is the entry point)

```python
def test_checkout(behavior):
    # no expected values — capture what the system actually returns
    behavior("checkout_total", checkout_total(CART), group="billing")
    behavior("daily_brief", summarize(items), group="ai", semantic=True)  # LLM text
```

- `name` doubles as a filename → `validate_name` enforces: no whitespace /
  control chars / path chars (`/\<>:"|?*`), ≤200 chars (and, on Windows, a store
  file path under 260 chars - checked at capture time), no `.`/`..`, must not
  end with `.`. Unicode (e.g. Hangul) is allowed.
- `payload` must be JSON-serializable (dict/list/str/number/bool/None); the error names the JSON path, the type and a conversion hint (`core/behavior._find_unjsonable`).
  `NaN`/`Inf` rejected (`NightwardError`). Duplicate names rejected.
- `group` is the blast-radius bucket (module / feature).
- `semantic=True` opts free-text output into the LLM judge (equivalence only —
  it can never approve). Files/documents go through `nightward.adapters`.

Examples: `example/test_app.py` (quickstart), `examples/petshop/test_shop.py`
(one cart touching three behaviors — the cascade demo),
`examples/newsroom/test_newsroom.py` (semantic judge).

---

## Known traps (frozen by hardening rounds — mind them when touching)

- **Skipped tests = fake REMOVED.** A skipped test captures nothing, so its
  behavior shows as REMOVED. The plugin records skipped/failed counts in
  `run_meta.json` and `nightward run` warns. Treat as false positive.
- **Failed tests = incomplete capture.** The plugin records failed + errored
  (setup error) counts; `recompute` stamps `report["incomplete"]`. `run` prints
  the summary then exits 1, `gate` exits 1, `status`/MCP report
  `"boundary": "incomplete"` when nothing else is unapproved (pytest exit 1
  passes through to recompute; 2·5 abort).
- **Windows cp949 consoles.** Hangul/emoji in captured payloads crash the
  legacy win32 writer. `cli.py` reconfigures stdout/stderr to UTF-8
  (`backslashreplace`) and `status --json` prints `ensure_ascii=False`.
  **Do not break this when adding output paths.**
- **Aborted runs keep the previous capture, but never its verdict.** The plugin
  flushes `pending/` only when pytest finished (exit 0/1). Flushing an
  interrupted/empty session would turn everything into REMOVED and `approve
  --all` would wipe the baseline. The runner deletes `report.json` on every
  unverified run (exit 2-5, timeout, run-token mismatch), so `gate` fails and
  `status`/MCP read "unknown" instead of the old "intact" (D12).
- **`approve --all` never approves REMOVED.** Skips and partial paths
  (`run tests/x.py`) produce fake REMOVED; bulk-approving them silently shrinks
  the baseline. Removals need `approve <name>` or `--all --include-removed`,
  which only drops a removal after a clean whole-suite run (D18 - inference
  from partial runs kept leaking): run_meta `clean` is true (the plugin's `_scope`:
  rootdir/testpaths only, no passthrough args or PYTEST_ADDOPTS, exit 0, every
  collected test passed, zero skipped/xfailed/deselected/errors; `clean_doubt` says
  why not; tests excluded at collection - collect_ignore/--ignore/a filtering hook,
  seen by Recorder hook wrappers - also make a run unclean) and the baseline `source`
  is in `completed` (D24). Legacy source-less baselines are never bulk-removable.
  Explicit removal is `approve --remove NAME...` / `--remove-group G` (D25: no run
  proof, but only names REMOVED in a fresh, reviewed report). `approve --all` backfills `source`
  into unchanged baselines (`Store.refresh_source`; never fingerprinted).
- **Rejections are binding for bulk approval.** `approve --all` skips any
  behavior whose current pending (or, for a removal, baseline) state matches its
  `rejected/` record (fingerprint + group) and lists it as "kept (rejected)".
  `approve <name>` overrides and deletes the record. `runner.classify` (compare +
  standing rejections) marks such changes `rejected`/`rejected_by` in the report,
  and flips a judged-SAME or an UNCHANGED (baseline == rejected payload, e.g. after a
  merge) back to CHANGED - a human rejection beats the judge (D19).
- **Deselected = not checked (D21).** The plugin records `deselected_ids`; a
  REMOVED whose baseline `source` was deselected (-k/-m) becomes `NOT_RUN` in
  `runner.classify`: listed in report `not_run`, excluded from `unapproved`, never
  removal proof. Skips/xfails and source-less baselines stay REMOVED (fail closed).
  Not checked is never done (D23): such a report's boundary is `partial`; `gate`
  exits 1 on it unless `--allow-not-run` (a CI-yaml opt-in); MCP can't waive it.
- **Decisions bind to what the human saw (D19).** Each report item carries a
  `token` (`baseline.change_token`: old state -> new state). `review` and `view`
  record the tokens whose diffs they displayed in `reviewed.json` (a scoped review =
  scoped mark; `run`/`report` print names only and mark nothing, D26); `approve` and `reject` refuse a name whose current token isn't
  there, and refuse a missing or stale report. `reject` only takes a name in the
  report (blast radius or judged-SAME). Any refused run (judge config, ledger, busy
  lock) invalidates report.json (`runner.refused_run_invalidates`).
- **`nightward report` = verdict without pytest.** `runner.recompute_capture` trusts
  `pending/` only if it matches run_meta `pending_digest` (written by the plugin in
  the same flush), then recomputes under the lock. `run PATH -- <pytest args>`
  passes args through (`--nightward-*` rejected; ours go last so they win).
- **Approve what was reviewed (D10).** CLI `run`, `review` and `view` (never MCP)
  write `reviewed.json` = the `pending_digest` a human just saw; `approve` refuses
  unless `digest(pending)` still matches, so an agent's run between review and
  approve can't slip unseen content into the baseline.
- **One writer per store (D11).** `core/lock.store_lock` creates `<store>/.lock`
  with O_EXCL (pid, host, command, since, token) around run (pytest child +
  recompute), approve and reject; a second writer gets a NightwardError naming
  the holder. The runner's token is its run id, so its own pytest child flushes
  under the parent's lock; a bare `pytest --nightward-record` takes the lock
  for its flush. A lock whose holder pid is dead on this host is taken over.
  Readers (gate/status/review/view) don't lock.
- **Pending is swapped, not rewritten in place.** `Store.replace_pending` builds
  `pending.tmp/` and renames it over `pending/`; baseline/report/meta writes go
  through `_atomic_write`. A torn capture reads as mass REMOVED.
- **No xdist during capture.** Each worker has its own Recorder, so the capture
  splits (the controller flushes nothing). The runner forces `-n 0`; the plugin
  turns `--nightward-record` + `-n` into a UsageError.
- **Reports can go stale.** `recompute` stamps `generated_at` + `baseline_digest`
  + `pending_digest`; if the baseline OR the capture changes afterwards (git pull,
  a direct `pytest --nightward-record`, a run killed before its report), the
  report is stale: `gate`/`review` exit 1, `status`/MCP report
  `"boundary": "stale"`, the dashboard shows a stale banner. A report without
  digests counts as stale. Code edits are not detected - re-run for a verdict.
- **A failed flush never replays the old capture.** `execute_run` passes a fresh
  `--nightward-run-id`; the plugin writes it into `run_meta.json` only after
  `pending/` was replaced. No matching token -> `NightwardError` and `report.json`
  is deleted. Payloads that can't be written (lone surrogates) fail at
  `behavior()` time via `canonical_json`.
- **Captured data is rich markup.** Every name/group/diff/path printed by the
  CLI goes through `rich.markup.escape` — `total[eur]` vanishes and `[/x]`
  crashes otherwise. Do this for any new output path.
- **Names are filenames on every OS.** `Store._file` validates every name (no
  path escape via CLI args); the Recorder rejects case-only collisions and
  Windows device names (`CON`, `NUL`, `COM1`…).
- **Default scrubbing is opt-out and counted.** `behavior(..., scrub=False)`
  skips every scrubber (payload still JSON-normalized); `scrub.disable_defaults()`
  turns off only the built-in timestamp/uuid rules (`scrub._reset()` re-enables).
  The Recorder counts default masks per behavior -> run_meta `scrubbed` ->
  `nightward run` prints a note and MCP `warnings.scrubbed`.
- **Reruns replace, they don't duplicate.** The `behavior` fixture calls
  `Recorder.begin(nodeid)` on every setup, dropping captures from an earlier
  attempt of the same test (pytest-rerunfailures). The same name from two
  *different* tests is still a duplicate error.
- **Diffs are bounded display, not verdicts.** `core/diff._line_diff` trims the
  common prefix/suffix, lets difflib align only a changed middle of <= 2,000
  lines, falls back to a positional (equal length) or coarse diff beyond that,
  and caps output at `MAX_DIFF_LINES`. Multi-line strings render as indented
  `"""` blocks. `approve --all` calls `compare(..., with_diff=False)`. Store
  files end with a newline (layout only - fingerprints hash the payload).
- **Scrub must not merge keys.** If text scrubbing collapses two dict keys into
  one (`<TIMESTAMP>`), `scrub` raises instead of silently dropping a value.
- **The verdict ledger is never silently reset.** A corrupt
  ledger file (e.g. merge-conflict markers) is a `NightwardError` naming the
  conflict. A persona entry that disagrees with a fresh ruling is rewritten and
  reported (`judge.ledger_mismatch`); it never decides a verdict (D22).
- User-causable errors must be **`NightwardError` + clear message**, never a
  traceback (CLI converts to exit 2).

---

## `nightward view` security model (read before touching view/)

Captured data is **never injected into HTML**. The generator copies
`view/assets/` (`index.html`/`app.js`/`style.css`) verbatim and emits data as a
separate `data.json`. The page `fetch`es it and renders via **`textContent`
only** — `innerHTML`/`insertAdjacentHTML` are **forbidden** (zero stored-XSS
surface; a guard test freezes this). CSP meta forbids inline script (hence the
external app.js). `fetch` is CORS-blocked on `file://`, so local viewing goes
through `--serve`. The dashboard is **read-only** (approve/reject stay in the
CLI). States intact/breached + `no-baseline`/`no-report` are first-class.
The clipboard is the other injection surface: behavior names come from test
code, and the copy-paste commands land in the approver's shell. Never build a
command from a raw name. `collect_data` emits `data.quoted` (per-shell forms
from `shellquote.py`, tested against real sh/PowerShell/cmd), and `app.js`
`cliCommand` uses only those; a name with no safe form gives no command.
Never publish a real `.nightward/` store to a public site — the only publish
path is clean-room synthetic data (`scripts/build_demo.py`).
Design rationale: `docs/superpowers/specs/2026-06-05-nightward-view-dashboard-design.md`.

---

## `nightward mcp` isolation model (read before touching mcp_server.py)

The MCP server lets AI agents *pull* the gate but never approve: only
`nightward_run` (capture+signal) and `nightward_status` (read) are exposed;
**`approve`/`reject` stay human-CLI-only** — trigger (AI) ≠ approval (human).
If they merge, the gate approves its own changes and dies (becomes a
changelog). `mcp_server._TOOLS` is the **single source** of the exposed
surface, and tests freeze that approve is absent
(`tests/test_mcp.py::test_isolation_*`). Same principle as view's
"read-only, approve is CLI-only". **The store is pinned too (R3-LLM-07)**: `serve()` resolves the store once
(`nightward mcp --dir`, default `.nightward`) into `_server_dir`; a tool call's `dir`
may only name it (else `NightwardError`), and results carry `store` (+ `path`). Called
as a library without `configure(dir=)`, the tools accept any store.
**No stdio pollution**: `run_tool` uses
`execute_run(capture_output=True)` so pytest stdout can't break the MCP
protocol channel (diagnostics to stderr only). `mcp` is an optional extra;
tool functions don't depend on the SDK, so they're testable without it.
**The judge is a committed project decision (D14)**: `nightward_run` takes no
judge argument (an agent could pick `persona:lenient`); it uses `nightward mcp
--judge`, else `[tool.nightward] judge` in the pyproject.toml nearest the STORE dir
(`config.project_judge`). Never `$NIGHTWARD_JUDGE` or run_meta: a CLI `--judge` is a
one-run override and must not leak into the agent's gate
(`tests/test_beta_judge.py::test_mcp_*`). run_meta's judge exists only so `approve`
recomputes with the verdicts of the report the human reviewed. The judge runs only on
behaviors approved as semantic; flipping the flag is CHANGED.
Design rationale: `docs/superpowers/specs/2026-06-07-nightward-mcp-agent-gate-design.md`.

---

## Scope guardrails (CONTRIBUTING.md — staying small is the value)

- **In scope**: the capture → blast-radius → approve → gate → loop-signal
  pipeline, the MCP agent gate (run/status exposed, approve isolated),
  robustness, pytest integration, input adapters (`adapters.py`: `from_file`
  is dependency-free for any format; pdf/docx/xlsx behind the `[docs]` extra),
  the semantic judge (`judge.py`: `anthropic:*` real models + `persona:*`
  key-free deterministic stand-ins; verdicts in the committed ledger).
- **Out (deliberately, for now)**: web UI expansion, multi-language runners,
  PR-comment bots. Open an issue before building.
- **Permanently OUT (gate suicide)**: exposing `approve`/`reject` over MCP,
  any automatic/policy-based approval engine. If the trigger (AI) and the
  approver (human) merge, the gate dies — see the MCP isolation model.
