# nightward

**Stop the exhausting infinite fix-loop.** *Gate every AI-made change against an approved behavior boundary.*

![90-second demo: an AI fixes one line, every test passes, nightward shows three customer-facing behaviors moved and stops the loop](docs/assets/demo.gif)

When an AI agent fixes A, it quietly moves B, C, and D. You discover the side
effects later, fix those, and spawn three more. nightward makes that blast radius
**visible immediately** and **blocks** anything that crosses an approved boundary —
so an autonomous loop finally has a definition of "done."

It is **not** a test generator. It captures what your system *already does*,
you approve it once, and from then on every change is gated against that snapshot.

## How it differs

| | |
|---|---|
| vs. NL→test generators | nightward **gates** changes, it doesn't author tests |
| vs. plain snapshot libs | nightward aggregates a **blast radius** + emits a machine **stop-signal** for agent loops |
| vs. ordinary regression tests | nightward judges AI/non-deterministic output and bounds the cascade |

## Quickstart

```bash
pip install -e .

# 0. once per repo: create .nightward/ and add .gitignore rules for its per-run
#    state (pending/, report.json, run_meta.json) and the dashboard (nightward-site/)
nightward init

# 1. capture current behavior and approve it as the baseline
nightward run example
nightward approve --all
git add .gitignore .nightward/baseline   # commit the approved baseline (= the boundary)

# 2. change the code, then re-run — the blast radius shows what moved
nightward run example
nightward review

# 3. gate it (exit 1 on any unapproved change) — wire this into CI / a ralph loop
nightward gate
nightward status --json     # {"boundary": "breached", "unapproved": 1, ...}

# 4. see the blast radius in a browser (read-only dashboard)
nightward view              # builds a static site + serves it on localhost
```

## Workflow

```
nightward init    create the store and add .gitignore rules (once per repo; `run`
                  warns while its per-run files are not git-ignored)
nightward run     re-run tests → capture → compute blast radius
nightward review  show changed behaviors with diffs; scope with `review NAME...` or
                  `--group G`; each diff shows 60 lines (`--max-lines N`, 0 = all)
nightward doctor  explain what moved in CHANGED behaviors; suggest scrub rules only
                  for values that are volatile by evidence (see below); takes the
                  same NAME... / --group scope as review
nightward approve promote pending behavior(s) into the baseline
                  (--all takes NEW/CHANGED; REMOVED needs a name or --include-removed).
                  It promotes only the capture a human last saw through `run`,
                  `review` or `view`; if anything captured again since (e.g. an
                  agent's nightward_run), it refuses until you review again
nightward reject  confirm a change as a real regression (boundary stays breached;
                  `approve --all` skips it as "kept (rejected)" while that payload
                  is pending - `approve <name>` overrides and clears the rejection).
                  Commit .nightward/rejected/ like the baseline, so a rejection
                  protects every clone and CI, not just your machine
nightward gate    exit 0/1 for CI and agent loops (1 also if the report is stale)
nightward status  boundary summary with the change list (--json: the machine
                  signal for agent loops): "intact" is the only
                  "done"; "breached", "incomplete" (capture tests failed/errored),
                  "stale" (baseline or capture changed since the last report -
                  re-run) and "unknown" (no report) are not
nightward view    build a static, read-only dashboard and view it in a browser
nightward mcp     stdio MCP server for AI agents: run + status, never approve
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
edited since then. "stale" only covers a baseline or capture that changed after the
run, never your source code. Both print `(as of the last run, <generated_at>; re-run
nightward run after code edits)` next to the verdict, and `status --json` and MCP
carry `generated_at`. Run `nightward run` again after every edit before trusting the
verdict.

**Merge conflicts in the baseline.** Two branches that approved the same behavior
differently leave git conflict markers in `baseline/<name>.approved.json`. Every
command then stops with `... has unresolved merge conflict markers`. Keep one side
(`git checkout --ours -- <file>` or `--theirs`), run `nightward run`, and
`nightward approve <name>` if the current behavior should be the new baseline.

A skipped, deselected (`-m`/`-k`), xfailed or errored test, or a partial path
(`nightward run tests/test_a.py`), captures nothing for the behaviors it didn't reach,
so they read as REMOVED. That is why `approve --all` leaves removals alone. Each
behavior records the test that captured it (`source`, never compared), and
`--include-removed` only drops a removal that a **whole-suite** run proves: the run
was not narrowed (no `-k`/`-m`, deselection or test-id argument, and every test file
any baseline points at was collected) and every test known to capture the behavior
ran to completion without capturing it. The rest are kept and listed with the reason
(a deleted test counts as "did not run" - approve such removals by name). Baselines
from before sources existed need a clean whole-suite run (nothing skipped, failed,
errored, deselected or xfailed); `approve --all` backfills `source` into unchanged
baselines whose capturing test moved. Capture runs in a single process: `nightward run` forces `-n 0` if
pytest-xdist is installed, and `--nightward-record` with `-n` is a usage error.
One writer per store: `run` (and MCP `nightward_run`), `approve` and `reject` hold
`.nightward/.lock`, so a second concurrent writer (`tox -p`, an agent next to a
human) fails fast and names the holder instead of corrupting the capture.

## What nightward normalizes (what counts as "the same payload")

Two captures are equal when their **normalized JSON** is equal. Know what the
normalization erases, because a change there never trips the gate:

- **Timestamps and UUIDs (default scrubbers).** Every ISO-8601 datetime with a `T`
  (`2024-01-05T09:00:00+00:00`) becomes `"<TIMESTAMP>"` and every UUID becomes
  `"<UUID>"`, anywhere in the payload (values *and* keys). This stops run-to-run
  noise, and the placeholders are visible in `baseline/*.approved.json`.
  `nightward run` prints how many values were masked. If datetimes or UUIDs are
  your **output** (deadlines, event times, `uuid5` ids), opt out:

  ```python
  behavior("sla_deadlines", deadlines, scrub=False)   # this behavior: no scrubbing at all

  # conftest.py - every behavior: built-in timestamp/uuid scrubbers off,
  # your own register/register_field rules still apply
  from nightward import scrub
  scrub.disable_defaults()
  ```

- **Dict key order.** Keys are sorted, so `{"b": 1, "a": 2}` equals `{"a": 2, "b": 1}`.
  If order is part of the contract (CSV column order, `df.to_dict("records")`
  feeding a positional loader), capture it explicitly: `list(df.columns)` or
  `df.to_csv(index=False).splitlines()`.
- **Key types and containers (JSON semantics).** Non-string keys become strings
  (`{3: 2}` equals `{"3": 2}`) and tuples become lists. Capture `type(k).__name__`
  or a list of pairs if the type matters.
- **Multi-line strings** (HTML bodies, CLI stdout, rendered manifests) are one JSON
  value. `review` and the dashboard diff them line by line, but the committed
  baseline file stores one escaped string, so its git diff is one long line -
  capture `text.splitlines()` when you want per-line git diffs too.

**Non-JSON values are rejected, never coerced.** A capture fails its test with the
path, the type and a fix, e.g. `behavior 'daily': payload is not JSON-serializable: value at
$.units is numpy.int64, which is not JSON - use .item() (or int()/float()/bool())`.
Common conversions:

| value | capture |
|---|---|
| numpy scalar (`np.int64`, `np.float32`, `np.bool_`) | `x.item()` |
| `np.ndarray`, `pd.Series` | `x.tolist()` |
| `pd.DataFrame` | `df.to_dict("records")` (or `df.to_csv(index=False).splitlines()`) |
| `datetime`, `date`, `pd.Timestamp` | `x.isoformat()` |
| `Decimal` | `str(x)` (keeps the exact digits) |
| `set` | `sorted(x)` |
| `bytes` | `x.decode()` or `x.hex()` |
| `NaN` / `inf` (e.g. the mean of an empty group) | `None` or a marker string such as `"NaN"` |
| dict with mixed key types (`{1: "a", "b": 2}`) | `str` keys |

`np.float64` is accepted as is: it subclasses Python's `float`. `np.float32` and the
numpy integer types don't, so convert them.

Diffs are for reading, never for the verdict: the verdict comes from
fingerprints, and a diff is capped at 2,000 lines (huge payloads end with a
"diff truncated" marker; very large scattered changes are compared by line
position), so `run`/`approve` stay fast on big captures.

Add your own rules for project-specific noise (prefer `register_field` - it
replaces a JSON value and can't corrupt the payload):

```python
from nightward import scrub
scrub.register_field("request_id")                 # mask this key at any depth
scrub.register(r'"ord_\d+"', '"<ORDER_ID>"')       # regex over the JSON text
```

`register()` patterns run over the payload's **canonical JSON text**, not over the
decoded strings: pretty-printed (`"key": "value"`, keys sorted), and inside a string
value every `"` is written `\"` and every newline `\n`. A pattern copied from what
the app emits (`name="csrf_token" value="..."`, `"request_id":"req_..."`) therefore
never matches, and `^`/`$` never see the lines of a multi-line body. Match the
escaped form instead:

```python
# <input type="hidden" name="csrf_token" value="3f9a..."> inside an HTML body
scrub.register(r'csrf_token\\" value=\\"[0-9a-f]{32}', r'csrf_token\\" value=\\"<CSRF>')
```

When the volatile part sits inside a string, masking it in the test before
capturing (`re.sub(...)` on the body) is often simpler. `nightward run` warns about
every `register`/`register_field` rule that matched nothing in that run
(`warning: scrub rule register(r'...') matched nothing in this run`), so a rule that
silently does nothing can't pass for handled noise.

`nightward doctor` sees one before/after pair, which is no evidence that a value is
noise, so it only calls a value volatile when the value itself shows it, and then
suggests the narrowest rule that hides exactly that:

| doctor sees | it suggests |
|---|---|
| a date-time or HTTP date | a `scrub.register(...)` pattern for that date shape |
| a random token behind a stable prefix (`chatcmpl-…`, `call_…`, a CSRF value in HTML) | a `scrub.register(...)` pattern anchored on that prefix, never the whole field or body |
| a Unix epoch under a time-like key (`created`, `updated_at`) | `scrub.register_field(key)`, only if that key is not stable in any other behavior |
| a float that moved only in its last digits | round it before capturing (`round(x, 10)`), no mask |
| a list with the same elements in a new order | sort it before capturing, no mask |
| a changed content hash, a type change, a new key, anything else | "looks like a real change": review, then approve or fix |

If a value marked as a real change changes again on a re-run with no code edits, it
is volatile: mask it at capture time in that test.

## Semantic judge (v0.2) — gate nondeterministic AI text

Free-text AI output breaches the fingerprint gate on every rewording (measured:
25/25 false positives on real data). Mark such behaviors `semantic=True` and pick
a judge model per run — the judge rules **equivalence only**; approval stays human:

```python
def test_summary(behavior):
    behavior("daily_summary", summarize(items), group="ai", semantic=True)
```

```bash
nightward run . --judge anthropic:claude-haiku-4-5   # real LLM (pip install nightward[judge])
nightward run . --judge persona:editor               # deterministic, key-free (see below)
NIGHTWARD_JUDGE=anthropic:claude-haiku-4-5 nightward run .   # or via env
```

The `persona:*` judges are deterministic and need no key. Both judging personas
**fail closed**: a change to any digit or number, sign, currency or unit symbol,
operator (`+ - < > = !=` ...), emoji, negation word, JSON key, or value type
(`49.99` vs `"49.99"`, a list vs a string) is DIFFERENT. In structured payloads
only string values are compared loosely.

| persona | rules SAME when... | use it for |
|---|---|---|
| `persona:editor` | only letter case, whitespace, or sentence punctuation (`. , ; : !` before a space or the end) differ. Every word must match; a unit after a number keeps its case (`5 mW` vs `5 MW`). | CI without a key: collapses cosmetic rewording only |
| `persona:lenient` | as editor, and ordinary words may also change (`went up` vs `rose`). Can pass `approved` vs `denied`. | tests and demos only, **never real gating** |
| `persona:strict` | never | forcing every mismatch to stay breached |

Any provider:model can plug in as a backend. Each ruling is recorded once per
fingerprint pair in `.nightward/judge_verdicts.json` — a **committed ledger**, so
the judge's own nondeterminism can't wobble the gate, fresh clones and CI replay
verdicts deterministically without a key, and every ruling lands in the PR diff
for human review: each entry records the behavior, model, verdict, reason, and
the old and new wording it ruled on (up to 1,000 chars each).

Rulings are visible wherever the verdict is:

- `nightward review` lists every behavior the judge ruled SAME, with its diff,
  even when the boundary is intact. A wrong SAME is a hole in the gate, so audit them.
- `status --json` (and MCP) carry `judged`, `judge_model` and `judge_reason` on each
  change, and a `judged_same` list of `{name, group, judge_model, judge_reason}`.
- The dashboard has a "ruled semantically SAME" section with the diffs.

A judge that can't rule (SDK not installed, no
`ANTHROPIC_API_KEY`, API error, unparseable reply) falls back to the exact
comparison, so the gate fails closed, and says so: `nightward run` prints
`warning: judge <spec> unavailable (<reason>); N semantic behavior(s) compared
exactly`, and the report and `status --json` carry
`"judge": {"spec", "unavailable", "compared_exactly"}`.

## AI agents (`nightward mcp`)

An agent loop needs a definition of "done" that it can check but not change.
`nightward mcp` is a stdio [MCP](https://modelcontextprotocol.io) server that lets
the agent **run** the gate and **read** its verdict. It can't approve anything.

```bash
pip install "nightward[mcp]"                      # the mcp 1.x SDK (2.x is not supported yet)
claude mcp add nightward -- nightward mcp         # e.g. Claude Code; run it in the project root
```

Other hosts take the usual JSON entry. Start the server in the project root: it
resolves `path` and `dir` against its own working directory.

```json
{"mcpServers": {"nightward": {"command": "nightward", "args": ["mcp"], "cwd": "/path/to/project"}}}
```

| tool | arguments | what it does |
|---|---|---|
| `nightward_run` | `path="."` (what pytest runs), `dir=".nightward"`, `timeout=600` (seconds; on expiry the capture is left untouched and the last report invalidated) | runs the tests, captures behaviors, recomputes the boundary |
| `nightward_status` | `dir=".nightward"` | reads the last run's verdict without running anything |

Both return the `status --json` shape: `boundary` (`intact` / `breached` /
`unknown`), `unapproved`, `changes` (`name`, `kind`, `group`, plus `judged`,
`judge_model`, `judge_reason` when a judge ruled), `judged_same`, `stale`,
`generated_at`, and `judge`. `nightward_run` adds `warnings`: `skipped`, `failed`,
`pytest_returncode`, and `pytest_output_tail` (pytest's last lines, so the agent can
see why tests failed). The agent is done when `boundary` is `"intact"` and `stale`
is false.

Rules for the loop:

- **Call `nightward_run` after every code edit.** `nightward_status` and `gate` only
  report the last run. They don't notice code edited since then, and `stale` only
  covers a baseline that changed after the run.
- **The agent can't approve.** `approve` and `reject` are not exposed. If the agent
  that makes a change could also approve it, the gate would turn into a changelog.
  A human approves with the CLI and commits the baseline.
- **The judge is the human's choice.** `semantic=True` behaviors are judged by
  `nightward mcp --judge <provider:model>`, else `$NIGHTWARD_JUDGE` in the server's
  environment, else the judge the last run used (for example the team's
  `nightward run --judge persona:editor`). The tool has no judge argument, so the agent
  can't pick a lenient judge, and it gets the same verdict as the CLI.

## Dashboard (`nightward view`)

![nightward dashboard — a breached boundary rendered from synthetic demo data](docs/assets/dashboard-light.png)

`nightward view` renders the blast radius as a self-contained static site — boundary
status, counts, and grouped diffs with copy-paste `approve`/`reject` commands. It is
**read-only** (decisions stay in the CLI) and **static** (no backend), so it also
deploys to GitHub Pages. Data is loaded via `fetch('./data.json')` and rendered with
`textContent` only — captured output never touches an HTML parser.

> ⚠️ The dashboard embeds your captured behaviors. **Do not publish a real `.nightward/`
> store to a public site.** The Pages workflow only publishes synthetic clean-room data
> (`scripts/build_demo.py`). The default output dir `nightward-site/` is in the rules
> `nightward init` writes, and `view` warns when its output is not git-ignored.

## Threat model — what this gate does and does not protect against

Be precise about the guarantee: **"boundary intact" means no *captured* behavior
changed** — nothing more. Read these four limits before trusting the green light:

1. **Coverage is your instrumentation.** The gate only sees what `behavior()`
   calls capture. An agent can break an uncaptured code path and the boundary
   stays intact. Instrument the behaviors you actually care about, and treat
   the gate as a tripwire on those — not as proof that "nothing broke."
2. **The gate is a convention, not a sandbox.** An agent with shell access can
   run `nightward approve`, copy `pending/` into `baseline/`, delete `behavior()`
   calls, or add scrub rules that mask a change. nightward does not try to
   police the filesystem — instead, every one of those bypasses leaves a
   visible trace in git. The enforcement point is **review**:
   - `baseline/*.approved.json`, `judge_verdicts.json`, scrub rules, and test
     files are code — review their diffs in every PR;
   - protect your main branch (required human review) and let CI re-run
     `nightward run && nightward gate` from source, so a locally forged
     "intact" can't merge itself;
   - never wire `approve` into the agent loop or CI (the MCP server
     deliberately doesn't expose it — keep your own glue to the same rule).
3. **A semantic judge can be wrong.** A false-SAME verdict is a hole in the
   gate. That's why judging is opt-in per behavior, the prompt is conservative
   (unsure → DIFFERENT), failures fall back to exact comparison, and every
   ruling is recorded in the committed `judge_verdicts.json` for human review.
   If a behavior must never be judged leniently, don't mark it `semantic=True`.
4. **Normalization defines "the same".** Default timestamp/UUID scrubbing, key
   order and JSON key coercion erase some differences by design — see
   [What nightward normalizes](#what-nightward-normalizes-what-counts-as-the-same-payload).

## Document & artifact inputs (`nightward.adapters`)

The gate only ever sees JSON, so any file format is one adapter away. Two levels:
`from_file` fingerprints the raw bytes of *anything* (zero dependencies — works
for formats nobody has a parser for), while content adapters extract what matters
so byte-level noise (re-saves, metadata stamps) doesn't breach the gate:

```python
from nightward.adapters import from_file, from_pdf, from_xlsx, from_text

def test_monthly_artifacts(behavior):
    behavior("report.content", from_pdf("out/report.pdf"), group="report")
    behavior("report.artifact", from_file("out/report.pdf"), group="report")
    behavior("export", from_xlsx("out/export.xlsx"), group="data")
    behavior("notice", from_text("out/notice.txt"), group="data")  # utf-8/cp949 auto
```

`from_text` hashes the decoded text with line endings normalized (CRLF, CR -> LF)
and a UTF-8 BOM dropped, so a file written on a Windows laptop and on a Linux CI
runner gates as equal. Content hashes (`text_sha256`, `content_sha256`, `sha256`)
are the gate's view of the content: never scrub them.

`from_pdf` / `from_docx` / `from_xlsx` need `pip install "nightward[docs]"`.
Validated on real-world files (Korean PDF/XLSX/DOCX/HWP/legacy-encoded TXT):
see `docs/experiments/2026-06-10-document-input-adapters.md`.

## v0 scope (intentionally small)

In: pytest capture, blast-radius diff, gate, loop signal, field-aware scrub + doctor,
static dashboard, MCP agent gate (run + status, no approve), LLM-as-judge semantic diff
(v0.2, multi-model).
Out (v1): PR-comment summaries, call-graph grouping, multi-language.
