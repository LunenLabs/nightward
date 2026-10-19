# Commands and workflow

[nightward](../README.md) reference. Every command, what it refuses and why, and how to use the pytest plugin directly in an existing CI job.

## Commands

```
nightward init    create the store and add .gitignore rules (once per repo; re-run it
                  after upgrading to migrate the rules it owns). `init` and `run` name
                  any rule that ignores what must be committed (baseline/, rejected/,
                  the judge ledger - e.g. a blanket `.nightward/`) and per-run files
                  that are not ignored
nightward run     re-run tests → capture → compute blast radius. Without a path it
                  runs the committed capture suite, `[tool.nightward] path` in the
                  pyproject.toml nearest the store (relative to that file), else "."
nightward report  verdict from an existing `pytest --nightward-record` capture
                  (no pytest run) - see "Using the plugin directly" below
nightward review  show changed behaviors with diffs; scope with `review NAME...` or
                  `--group G`; each diff shows 60 lines (`--max-lines N`, 0 = all).
                  `--summary` lists each changed path once instead, list indices
                  folded (`cases[*].correct  78/80 changed  "ERR" -> 41 distinct
                  values: ...`); NEW/REMOVED behaviors get their value distribution.
                  It marks what it showed as reviewed, like a capped diff
nightward doctor  explain what moved in CHANGED behaviors; suggest scrub rules only
                  for values that are volatile by evidence (normalization.md); takes
                  the same NAME... / --group scope as review
nightward approve promote pending behavior(s) into the baseline
                  (--all takes NEW/CHANGED and never drops a REMOVED behavior;
                  `approve A B C` works like --all limited to those names and refuses
                  REMOVED ones, while one name always applies, even a removal or a
                  rejection; `approve --group G` works like --all limited to group G,
                  for groups too big to list on one command line; `approve --remove
                  NAME...` and `--remove-group G` drop exactly those REMOVED behaviors -
                  removal is always this explicit human act on what review showed).
                  It promotes only changes a human was shown, exactly as shown,
                  with its diff by `review` or `view` (`run` lists names only, so it
                  counts as no review): a scoped `review --group G`
                  covers only G, and a change captured again since (your own
                  re-run or an agent's nightward_run - the refusal names the run
                  that took the capture) or a stale report (a `git pull` moved
                  the baseline) is refused until you run/review again
nightward reject  confirm a change you reviewed as a real regression. It takes only
                  a change in the last report (or a judged-SAME ruling, which the
                  rejection overrules: the behavior is unapproved again) and keeps
                  the boundary breached; `approve --all` skips it as "kept
                  (rejected)" while that payload is pending, and `run`, `review`,
                  `status` and the dashboard mark it "rejected by <who>" -
                  `approve <name>` overrides and clears the rejection. Commit
                  .nightward/rejected/ like the baseline, so a rejection protects
                  every clone and CI, not just your machine
nightward gate    exit code for CI and agent loops: 0 intact; 1 breached, partial
                  (behaviors not checked), stale or incomplete; 2 no store, no report
                  (no run yet, or the last run aborted) or another error. Only 0 is a
                  pass. `--allow-not-run` passes "partial" in a deliberately narrowed job.
                  `--run` captures first (`nightward run`, same path and judge), then
                  gates on that fresh verdict - the one-command form for loops and
                  hooks; `--run --path P` runs P instead of the committed suite
nightward status  boundary summary with the change list (--json: the machine
                  signal for agent loops): "intact" is the only
                  "done"; "breached", "partial" (approved behaviors not checked:
                  their test was deselected), "incomplete" (capture tests failed/errored),
                  "stale" (baseline, capture or rejections changed since the last report -
                  re-run) and "unknown" (no report) are not
nightward view    build a static, read-only dashboard and view it in a browser
nightward mcp     stdio MCP server for AI agents: run + status, never approve
nightward --version  the installed version (put it in bug reports and CI logs)
```

Every command uses the store at `./.nightward` (or `--dir`), so run them from the
project root. A store that isn't there is an error that says where nightward looked,
and points at `../.nightward` when you are in a subdirectory, rather than reading as
empty or "intact". `status` still prints `unknown` (exit 0), with that note on stderr.

A failing or erroring capture test means its behaviors are missing from the blast
radius, so the run is **incomplete**: `nightward run` prints the summary and exits 1,
the report records `incomplete: {"failed": n, "errors": m}`, and `gate` exits 1 until
a clean run.

`gate` and `status` read the report of the **last run**; they don't notice code
edited since then. "stale" only covers a baseline, capture or rejection that changed after the
run, never your source code. Both print `(as of the last run, <generated_at>; re-run
nightward run after code edits)` next to the verdict, and `status --json` and MCP
carry `generated_at`. Run `nightward run` again after every edit before trusting the
verdict, or `nightward gate --run`, which does both.

**Baselines made somewhere else.** Each capture records the OS and Python version it
ran on, and `approve` keeps that record in the baseline file (`"env"`, never part of
the fingerprint). When a CHANGED behavior was approved on another OS or Python than
the run that changed it, `run`, `review`, `gate` and `status` print a note naming
both, and `status --json`/MCP carry `env_mismatch`. It never changes the verdict: a
Windows path or line ending in a payload is still a change. Fix the payload
([payload design](payload-design.md#4-leave-the-machine-out-of-the-payload)) or
approve from the environment CI gates on.

**Merge conflicts in the baseline.** Two branches that approved the same behavior
differently leave git conflict markers in `baseline/<name>.approved.json`. Every
command then stops with `... has unresolved merge conflict markers`. Keep one side
(`git checkout --ours -- <file>` or `--theirs`), run `nightward run`, and
`nightward approve <name>` if the current behavior should be the new baseline.
Judge rulings are stored one file per ruling (`.nightward/judge/`), so two branches
that each record a ruling merge cleanly. A conflict there means both branches ruled
on the same pair differently: read both sides and keep one. The single-file
`judge_verdicts.json` that older versions wrote is still read (its entries replay only
when they describe the pair, see [the semantic judge](semantic-judge.md)); if it conflicts, keep
both sides' entries (each entry is an independent ruling).

A skipped, deselected (`-m`/`-k`), xfailed or errored test, or a partial path
(`nightward run tests/test_a.py`), captures nothing for the behaviors it didn't reach,
so they read as REMOVED - and so does a capture that moved into a test this run
left out (`collect_ignore`, `norecursedirs`, `__test__ = False`, a collection hook, a
hidden directory ...). No run can tell those apart from a real removal, so **no bulk
approval ever drops a behavior**: `approve --all` and `approve --group G` keep every
REMOVED behavior and say why it reads REMOVED (its recorded test, `source`, did not
run, or ran and passed without capturing it; tests the run skipped, deselected or did
not collect). Those hints help you decide; they never authorize a deletion. A removal
you intend is an explicit decision: review it, then `nightward approve --remove
NAME...` or `--remove-group G` (one `nightward approve <name>` also works). The old
`--include-removed` now refuses and points at those commands.
`approve --all` backfills `source` into unchanged baselines whose capturing test moved.
Capture runs in a single process: `nightward run` forces `-n 0` if
pytest-xdist is installed, and `--nightward-record` with `-n` is a usage error.
One writer per store: `run` (and MCP `nightward_run`), `approve` and `reject` hold
`.nightward/.lock`, so a second concurrent writer (`tox -p`, an agent next to a
human) fails fast and names the holder instead of corrupting the capture.

## Using the plugin directly in an existing pytest job

If CI already runs pytest (with its own `-m`, `-p`, `--timeout` ...), capture in that
same run and turn it into a verdict without running the suite twice:

```bash
pytest -m "not gpu" --nightward-record     # the plugin writes .nightward/pending + run_meta
nightward report                           # verdict from that capture (no pytest run)
nightward gate --allow-not-run             # this job deliberately skips the GPU tests
```

`nightward report` trusts the capture only if `run_meta.json` proves `pending/` is
exactly what a complete pytest session flushed; a failed/errored session exits 1 like
`run`. A behavior whose recorded test you deselected (`-k`/`-m`) is **not checked**,
not REMOVED: `run`, `report`, `gate`, `status --json` (`not_run`, `narrowed: true`) and
the dashboard list it, and it doesn't count as unapproved. But "not checked" is never
"done": the boundary is **`partial`**, and `gate` exits 1 on it unless the job passes
`--allow-not-run`. That flag belongs in the CI yaml of a deliberately narrowed job
(above, a GPU-less PR runner) whose full run happens elsewhere (nightly, or
`nightward run`); then a test that the change itself deselects - an added `-k`/`-m` in
`addopts`, a new `@pytest.mark.slow` on the broken test - shows up in that job's
`not_run` list, and the full run still breaches. MCP has no such option: for an agent,
`partial` means not done. This needs the recorded
test in the baseline (`source`, written on approve): approve once from a full run.
Baselines from before sources existed, and skipped or xfailed tests, still read as
REMOVED (fail closed). Or let nightward drive pytest and pass the arguments through:
`nightward run tests -- -m "not gpu" -p no:randomly`.
