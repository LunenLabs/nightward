# Changelog

All notable changes to nightward are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: a minor version may change
behavior; every such change is listed under "Upgrading").

## [Unreleased]

### Fixed

- **The Anthropic judge works on current models.** It no longer sends
  `temperature=0`, which current Claude models reject with a 400 (the ledger
  already makes a model's ruling repeatable by replaying it). It reads the first
  text block of the reply instead of the first block, which on current models is a
  thinking block, and gives the reply room for that thinking. A refusal makes the
  judge unavailable, so the behavior falls back to the exact comparison. Examples
  now use `anthropic:claude-haiku-5-5`.

### Added

- **[Recipe: make "only a human approves" hold on GitHub](docs/recipes/protect-the-baseline.md):**
  CODEOWNERS for `baseline/`, `rejected/`, `judge/`, `pyproject.toml` and
  `conftest.py`, required code-owner review, and an agent identity that isn't an
  owner. The README and the threat model point at it.

### Changed

- The README headline says what holds: the agent can run the gate, and opening it
  takes a human-approved baseline diff. A new section compares nightward with
  syrupy, inline-snapshot and ApprovalTests, including where they are better, and
  the `persona:*` judges are described as stand-ins that don't forgive rewording.
- The test suite runs in parallel (`pytest -q -n auto`) in CI and in
  CONTRIBUTING: about 5 minutes instead of 15.
- `nightward/cli.py` (1,500 lines) is now the package `nightward/cli/`, one module
  per command family. `nightward.cli:app` and every command's help are unchanged.
- Tests are grouped by area (`tests/core`, `tests/gate`, `tests/judge`, ...) instead
  of by the review round that found them.

## [0.3.3] - 2026-10-15

From a user's first review of a large capture. The gate decides exactly as before.

### Added

- **`nightward review --summary`.** Each changed path once, list indices folded into
  `[*]`, with its old -> new values counted: `cases[*].correct  78/80 changed
  "ERR ImportError" -> 41 distinct values: ...`. NEW and REMOVED behaviors show their
  value distribution. An 800-line diff becomes a few lines a reviewer actually
  reads; it marks what it showed as reviewed, like a capped diff. A capped diff now
  points at it.
- **Capture environment.** Every capture records its OS and Python version, and
  `approve` keeps it in the baseline file (`"env"`, not fingerprinted). A CHANGED
  behavior approved on another OS or Python gets a note in `run`, `review`, `gate`
  and `status`, and `env_mismatch` in `status --json` and MCP - suspect the
  environment before the code. Never a verdict.
- **`nightward gate --run`** captures first, then gates on that verdict: one command
  that can't pass on a run older than the code.
- **[Designing payloads a human can review](docs/payload-design.md):** aggregate by
  cause and keep the members, record each input once, structured fields over free
  text, keep the machine out of the payload.

### Upgrading

- Baseline files you approve from now on carry an `"env"` key: a one-time line in
  their git diff. Older nightward versions ignore it, and baselines without it are
  never reported as approved elsewhere.

## [0.3.2] - 2026-10-15

Two ways to adopt the gate without wiring it by hand. No change to how the gate
decides.

### Added

- **GitHub Action.** `uses: LunenLabs/nightward@v0.3.2` installs that version of
  nightward, runs your tests, writes the verdict and the diffs that moved to the
  job summary, and fails the job unless the boundary is intact. It never approves.
  Inputs: `path`, `dir`, `pytest-args` (one per line), `allow-not-run`, `version`,
  `install`, `summary`; output: `boundary`. CI runs it on Ubuntu and Windows
  against the committed example and a seeded regression.
- **Claude Code plugin.** `/plugin install nightward --marketplace LunenLabs/nightward`
  adds the MCP server (run + status, never approve), a `gate-loop` skill that has
  Claude run the gate after its edits and hand intended changes to a human, and
  `/nightward:status`.

### Changed

- The README comparison table has a header row, and the CI recipe uses the action.

## [0.3.1] - 2026-10-14

A documentation and packaging patch. No change to how the gate decides.

### Fixed

- The PyPI project page still said nightward was "not on PyPI yet" and showed the
  git pre-release install lines: PyPI keeps the README a release was built with.
  0.3.1 ships the README with plain `pip install nightward`.
- The clean-room demo site (`scripts/build_demo.py`, used for the Pages demo and the
  README screenshots) showed the absolute temp path of the machine that built it,
  including its user name. It now shows `.nightward`.

### Changed

- Refreshed the dashboard screenshot and the demo GIF for the 0.3 UI. The GIF adds
  the core rule: the agent can run the gate over MCP, only a human can approve,
  and only what `review` showed.

## [0.3.0] - 2026-10-13

The first release meant for other people's repositories. 0.3.0 is the 0.2 core
after a hardening pass and five rounds of beta review. Almost every change closes
a way the gate could pass, approve or remove something that no human looked at.
The rule behind them: **fail closed**. When nightward can't tell, it says "not
done" and names the cheap fix.

### Upgrading from 0.2

0.2 was installed from git (`nightward --version` did not exist yet), so check
with `pip show nightward`. Do these steps once, in order:

1. **Upgrade** the install (`pip install -U nightward`, or
   `pip install -U "git+https://github.com/LunenLabs/nightward"` before the PyPI
   release).
2. **Run `nightward init`** in the project root and commit `.gitignore`. On an
   existing store it keeps the store as it is and migrates the ignore rules: it
   removes the `.nightward/rejected/` rule (rejections are committed now) and
   adds `nightward-site/`, `.nightward/.lock`, `.nightward/.lock.takeover` and
   `.nightward/reviewed.json`.
3. **Commit `.nightward/rejected/` and `.nightward/judge/`** with `baseline/`.
   They are team decisions, so CI and every clone have to see them. Rejection
   files that 0.2 wrote have no reviewer recorded, so they are **audit-only**:
   they never breach or block, and `init` and `run` list them. Delete them, or
   `nightward reject` again to make a rejection binding.
4. **Move a standing judge into `pyproject.toml`.** If CI or the MCP server got
   its judge from `--judge` or `$NIGHTWARD_JUDGE`, write it as
   `[tool.nightward] judge = "provider:model"`. `--judge` and `$NIGHTWARD_JUDGE`
   now apply to one CLI run only and are never remembered. `nightward mcp`
   ignores `$NIGHTWARD_JUDGE`; it uses the committed setting or
   `nightward mcp --judge`.
5. **Move scrub rules into test code.** `scrub.register`, `register_field` and
   `disable_defaults` must now be called directly in a `conftest.py` or a test
   module. A call from any other file (product code, a shared helper) raises a
   `NightwardError` naming the file and line. A rule applies only to tests under
   the registering file's directory. For a suite-wide rule, use the root
   `conftest.py`.
6. **Run `nightward run` once.** A 0.2 report has no capture digest, so it reads
   as **stale**: `gate` exits 1 and `status` says `stale` until you run again.
7. **Review the one-time diffs that run shows, then approve them:**
   - `from_text` captures now normalize CRLF/CR line endings and drop a UTF-8
     BOM before hashing, and `lines` counts lines, not newline characters. A
     baseline from a CRLF or BOM file, or from any file ending in a newline,
     shows CHANGED once.
   - Persona judges (`persona:*`) rule again on every run under the current,
     stricter rules. A pair that 0.2 ruled SAME can now be DIFFERENT, and run
     writes a fresh ruling file into `.nightward/judge/` (commit it).
   - Model rulings in 0.2's `judge_verdicts.json` don't record the wording they
     ruled on, so they are not replayed. Each pair is ruled again (this needs the
     API key once), or compared exactly when no judge is available.
   - `approve --all` writes the capturing test (`source`) into baseline files,
     which shows up once in `git diff`. Fingerprints don't change.
8. **Update scripts and CI** for the behavior changes below. The loop is now
   **run, review, approve**. `approve` refuses anything `review` (or `view`)
   didn't show you.

Behavior changes a 0.2 user will hit:

- **`run` exit codes.** 0 when the capture completed (intact *or* breached; the
  verdict comes from `gate`). **1 when a capture test failed or errored** (the run
  is *incomplete*; 0.2 only warned). 2 when pytest aborted, timed out or the run
  was refused. An aborted run now **deletes the last report**, so `gate` can't
  pass on an older verdict.
- **`gate` exit codes.** 0 intact. 1 breached, **partial** (approved behaviors
  not checked because `-k`/`-m` deselected their test), stale, or **incomplete**.
  2 no store, no report (no run yet, or the last run aborted) or another error.
  Only 0 is a pass. A deliberately narrowed CI job can pass `partial` with
  `gate --allow-not-run`.
- **`status --json` / MCP `boundary`** can now be `partial`, `incomplete` and
  `stale` as well as `intact`, `breached` and `unknown`. Treat anything but
  `intact` as not done. A check for `== "breached"` misses the new values.
- **`semantic` is part of the approved behavior.** Turning `semantic=True` on
  or off for an approved behavior is a CHANGED that needs approval, and the judge
  runs only when both the baseline and the capture are semantic (0.2 judged
  whenever the capture was).
- **Approve only what you reviewed.** `run` lists names only and marks nothing
  reviewed. `review` (for what it printed) and `view` (the dashboard build) do.
  `approve` promotes a change only if its current capture is the one a human was
  shown. It refuses a change captured again since the review (by you or by an
  agent's `nightward_run`), and it refuses a stale report (for example, after a
  `git pull` moved the baseline).
- **Removals are always an explicit act.** `approve --all`, `approve --group G`
  and multi-name `approve A B` never drop a REMOVED behavior. They list each one
  and say why it reads REMOVED. Remove with `approve --remove NAME...`,
  `approve --remove-group G`, or a single `approve NAME`.
  **`--include-removed` now refuses** and points at those commands.
- **`approve --all` skips standing rejections** ("kept (rejected)").
  `approve NAME` overrides one and clears it.
- **`reject`** takes only a change from the last reviewed report. It also works
  on a judged-SAME behavior; the human rejection overrules the judge.
- **One writer per store.** `run`, `approve` and `reject` hold
  `.nightward/.lock`. A second concurrent writer fails fast and names the holder.
- **Arguments are taken literally on Windows.** `$VAR`, `%VAR%`, `~` and globs
  are no longer expanded inside nightward's arguments.

### Added

- `nightward review NAME...` / `--group G` / `--max-lines N`, and the same
  scoping for `doctor`.
- `nightward approve A B C` (several names), `approve --group G`,
  `approve --remove NAME...` and `approve --remove-group G`.
- `nightward report`: the verdict from a capture that `pytest --nightward-record`
  already wrote in your own CI job, without running the suite twice.
- `nightward run PATH -- <pytest args>` passes arguments through to pytest.
- `nightward gate --allow-not-run` for deliberately narrowed CI jobs.
- `nightward --version`.
- `nightward mcp --dir` and `--judge`. The server pins its store at start and
  names it (`store`) in every result.
- Judge configuration in `[tool.nightward] judge`, read from the `pyproject.toml`
  nearest the store.
- Default scrubbing opt-outs: `behavior(..., scrub=False)` and
  `scrub.disable_defaults()`. `run` reports how many values the default
  scrubbers masked.
- Custom scrub rules are reported: per rule, the values and behaviors it
  replaced (when that count changes), rules that matched nothing, and the active
  rules in `review`, `status`, `status --json`, MCP and the dashboard.
- Judged-SAME rulings are listed in `run`, `review`, `status`, `status --json`
  (`judged_same`, `judge_model`, `judge_reason`, `judge_replayed`) and the
  dashboard. `run` names the ruling files it added and says to commit them.
- `not_run` / `narrowed` in the report, `status --json` and MCP, for behaviors
  whose test was deselected.
- `status` prints the change list. `gate` and `status` print the time of the
  verdict ("as of the last run").
- `doctor` explains float noise (with an evidence-based tolerance), reordered
  rows and columnar frames, elements added to or removed from lists, JSON inside
  strings, Unicode normalization changes and signed zero, and suggests only rules
  that are checked to equalize both samples.
- `review` and the dashboard make invisible and look-alike character changes
  visible (`U+00A0 NO-BREAK SPACE -> U+202F NARROW NO-BREAK SPACE`).
- The dashboard has a shell picker for its copy-paste commands
  (bash/PowerShell/cmd.exe), a group "approve this group" chip, "replayed" badges
  on ledger rulings, and states for partial, incomplete, stale and no verdict.
- A CI recipe: `docs/recipes/ci-github-actions.md`.

### Changed

- The judge ledger is one JSON file per ruling in `.nightward/judge/`, so two
  branches merge it without conflicts. The single-file `judge_verdicts.json` is
  still read.
- Persona rulings are recomputed on every run, and their ledger entries are a
  record only. Model rulings are replayed only when the entry describes exactly
  the pair being judged (its file name, behavior, model and both wordings match).
- The semantic judge runs only when the baseline *and* the capture are
  `semantic=True`.
- Store files are written with LF line endings on every OS.
- Diffs are bounded (2,000 lines, with a "truncated" marker), and multi-line
  strings diff line by line.
- A non-JSON payload fails its test with the path, type and fix
  (`value at $.units is numpy.int64 ... use .item()`). On Windows, a name that
  would push the store file past MAX_PATH fails at capture time.
- Commands that read the store refuse a missing store and point at
  `../.nightward` when you run from a subdirectory, instead of reading it as empty.
- `pytest` reruns (for example, `pytest-rerunfailures`) replace that test's
  earlier captures instead of failing as duplicates.
- Default scrubbers stay on, but they are counted and documented
  ([docs/normalization.md](docs/normalization.md)).

### Fixed

- A crash while writing the capture, or a run killed between capture and
  report, no longer leaves an old "intact" verdict in place. Runs are tied to
  their capture by a run token and to the store by digests.
- When pytest can't load the suite, `nightward run` reports the real cause (for
  example, a `conftest.py` import error) instead of always suggesting a missing
  install. A run path that doesn't exist is named before pytest starts.
- The pytest child gets no stdin, so `nightward_run` can't hang an MCP stdio
  session.
- A reader that closes the pipe early (`nightward status | head`) and help into
  a closed pipe exit quietly instead of printing a traceback.
- git output (user name, store paths) is read as UTF-8, so non-ASCII names work
  on Windows.
- Stale-lock takeover is serialized, and releasing a lock survives Windows
  sharing violations.
- Unchanged capture files are reused, and `report` reads `pending/` once
  (faster on big stores).

### Security (fail-closed hardening)

- **Approvals bind to what a human saw.** Review marks record the exact
  fingerprints displayed. A capture taken after the review can't ride along with
  an approval or a rejection.
- **No inferred removals.** A skipped, deselected, uncollected or excluded test
  looks exactly like a removed behavior, so only an explicit human command
  deletes from the baseline.
- **Incomplete, partial and stale are never green.** Failing capture tests,
  deselected behaviors and a store that moved after the report all keep `gate`
  at exit 1.
- **Shared decisions.** Rejections and judge rulings are committed. A rejection
  pulled in after your review makes the report stale.
- **The judge is a committed project decision.** An agent can't choose or loosen
  it: MCP has no judge argument and ignores `$NIGHTWARD_JUDGE` and `--judge`
  overrides.
- **MCP can't be pointed at a forged store.** A tool call's `dir` may only name
  the server's own store, and a server started in a subdirectory refuses rather
  than create a second, empty store. `approve` and `reject` remain unexposed.
- **Persona judges fail closed.** Any change to a digit or number (including
  anything between two digits), sign, currency or unit symbol, operator, emoji,
  negation (English, Korean, Japanese, Chinese), JSON key or value type is
  DIFFERENT. Identifiers, code and JSON are compared literally, and only prose is
  compared loosely.
- **The model judge's prompt** inserts each output once, fenced by a fresh
  random delimiter, and is built without chained string replacement.
- **Scrub rules are test-owned and scoped.** Rules from product code are
  refused, and a rule whose replacement isn't a `<PLACEHOLDER>` is called out as
  a rewrite.
- **The dashboard quotes every behavior name** for the selected shell and offers
  no command when a name has no safe form. Its serve hint binds to loopback only.
- **Windows argument expansion is off**, so a quoted `'price.$region'` can't
  approve a different behavior.

## [0.2.0]

Never published to PyPI; installed from git.

- Semantic judge (`--judge provider:model`, `persona:*` key-free judges) for
  `semantic=True` behaviors, with a committed verdict ledger.
- `nightward view`: a static, read-only dashboard.
- `nightward mcp`: a stdio MCP server that exposes run and status, never approve.
- Document adapters (`from_file`, `from_text`, `from_pdf`, `from_docx`,
  `from_xlsx`).

[0.3.3]: https://github.com/LunenLabs/nightward/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/LunenLabs/nightward/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/LunenLabs/nightward/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/LunenLabs/nightward/compare/900b8d5...v0.3.0
