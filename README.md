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
nightward report  verdict from an existing `pytest --nightward-record` capture
                  (no pytest run) - see "Using the plugin directly" below
nightward review  show changed behaviors with diffs; scope with `review NAME...` or
                  `--group G`; each diff shows 60 lines (`--max-lines N`, 0 = all)
nightward doctor  explain what moved in CHANGED behaviors; suggest scrub rules only
                  for values that are volatile by evidence (see below); takes the
                  same NAME... / --group scope as review
nightward approve promote pending behavior(s) into the baseline
                  (--all takes NEW/CHANGED; REMOVED needs a name or --include-removed;
                  `approve A B C` works like --all --include-removed limited to those
                  names, while one name always applies, even a removal or a rejection).
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
Judge rulings are stored one file per ruling (`.nightward/judge/`), so two branches
that each record a ruling merge cleanly. A conflict there means both branches ruled
on the same pair differently: read both sides and keep one. The single-file
`judge_verdicts.json` that older versions wrote is still read; if it conflicts, keep
both sides' entries (each entry is an independent ruling).

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

## Using the plugin directly in an existing pytest job

If CI already runs pytest (with its own `-m`, `-p`, `--timeout` ...), capture in that
same run and turn it into a verdict without running the suite twice:

```bash
pytest -m "not gpu" --nightward-record     # the plugin writes .nightward/pending + run_meta
nightward report                           # verdict from that capture (no pytest run)
nightward gate
```

`nightward report` trusts the capture only if `run_meta.json` proves `pending/` is
exactly what a complete pytest session flushed; a failed/errored session exits 1 like
`run`. Or let nightward drive pytest and pass the arguments through:
`nightward run tests -- -m "not gpu" -p no:randomly`. Either way a narrowed run
(`-k`, `-m`, deselection, a test id) never proves a removal.

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

  # the root conftest.py - every behavior: built-in timestamp/uuid scrubbers off,
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

**Names are file names.** A behavior name becomes `<store>/baseline/<name>.approved.json`:
no whitespace or path characters, at most 200 characters, and on Windows the whole store
file path must stay under 260 characters (Git for Windows can't add longer paths without
`core.longpaths`). A name that would cross that limit fails its test at capture time -
keep names short when the project lives in a deep directory.

**Non-JSON values are rejected, never coerced.** A capture fails its test with the
path, the type and a fix, e.g. `behavior 'daily': payload is not JSON-serializable: value at
$.units is numpy.int64, which is not JSON - use .item() (or int()/float()/bool())`.
Common conversions:

| value | capture |
|---|---|
| numpy scalar (`np.int64`, `np.float32`, `np.bool_`) | `x.item()` |
| `np.ndarray`, `pd.Series` | `x.tolist()` |
| `pd.DataFrame` | `df.to_dict("records")` (or `df.to_csv(index=False).splitlines()`) |
| `date` | `x.isoformat()` |
| `datetime`, `pd.Timestamp` | `x.isoformat()`, but an ISO date-time is masked as `"<TIMESTAMP>"` by the default scrubber: if it is your output (a due date, an event time), also pass `scrub=False` (or call `scrub.disable_defaults()`) |
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

Some changes print the same on both sides: NO-BREAK SPACE vs NARROW NO-BREAK SPACE
after a CLDR upgrade, a zero-width space or bidi mark, doubled or trailing
whitespace, a Cyrillic `а` in place of a Latin `a`, or `−` (minus) in place of `-`.
For such a -/+ pair, `review` and the dashboard escape only the characters that
differ (`"1 234"` -> `"1 234"`) and add a `? invisible or look-alike
change: U+00A0 NO-BREAK SPACE -> U+202F NARROW NO-BREAK SPACE` line, and `doctor`
names them in its note. This is display only and never affects the fingerprint.
The committed baseline file stores the raw characters, so its git diff still
looks unchanged; use `nightward review` to read it.

Add your own rules for project-specific noise (prefer `register_field` - it
replaces a JSON value and can't corrupt the payload):

```python
from nightward import scrub
scrub.register_field("request_id")                 # mask this key at any depth
scrub.register(r'"ord_\d+"', '"<ORDER_ID>"')       # regex over the JSON text
```

**Scope follows the conftest.py, like its fixtures.** A rule registered in a
`conftest.py` (also through a helper that conftest calls) applies only to behaviors
captured by tests under that conftest's directory. Rules in the root `conftest.py`
cover the whole suite; `scrub.register_field("token")` in `services/orders/conftest.py`
masks orders' tokens but never a `token` field in `services/billing`. The scope also
doesn't depend on which directories a run collected, so `nightward run services/billing`
captures exactly what the whole-suite run does. `scrub.disable_defaults()` is scoped
the same way. Rules registered anywhere else (a test module, a plugin) are global.
`nightward doctor` names the conftest.py each suggested rule belongs in.

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
noise. It calls a value volatile only when the value itself shows it. A date or an
order can be the product (a deadline, an event sequence), so those look like a real
change first:

| doctor sees | it says | it suggests |
|---|---|---|
| a random token behind a stable prefix (`chatcmpl-…`, `call_…`, `req_…`, a CSRF value in HTML) | `~` volatile | a `scrub.register(...)` pattern anchored on that prefix, with a base62 class and open length (`[0-9A-Za-z]{8,}`), never the whole field or body |
| a date-time, HTTP date or Unix timestamp (also as a string, e.g. `X-RateLimit-Reset`) | `*` looks like a real change | only *if it is not part of the contract*: `scrub.register_field(key)` for that key, or a pattern anchored on the text before it; for a date in a list, "mask it at capture time" (no global date pattern) |
| several dates that all moved by the same amount | `*` looks like a real change ("all 2 values moved by -1 day") | nothing |
| a list with the same elements in a new order | `*` looks like a real change | only *if it is not part of the contract* (e.g. a set): sort it before capturing |
| the same rows in a new order with float noise in some values (an unordered `GROUP BY` on a parallel engine) | `*` looks like a real change, naming the columns with noise (`$[*][2]`) | only *if order is not part of the contract*: sort (`ORDER BY` a key) and round to the number of digits it names, checked to make both samples equal |
| a float within a few ULPs: float64, or float32 values such as embeddings | `~` float noise | round before capturing, to the most significant digits (at most 12, or 6 for float32) that make every drifted value in that path equal, e.g. `float(f"{x:.4g}")`; no mask. Rounding lowers the odds of a flip but can't rule it out: a value near a rounding boundary can still flip on another machine. For vectors, capture what the product uses (top-k ids, a ranking). Integral floats and deltas of 1 or more are never noise |
| rounded floats (5+ significant digits, 4+ decimals) that differ by 1 in the last digit | `*` looks like a real change | *if the capture rounds float noise*, these are rounding-boundary flips: fewer digits (checked on these values), or capture what the product uses |
| a string holding JSON (a tool call's `arguments`) whose parsed value is unchanged | `~` formatting only | capture `json.loads(...)` of it; when the parsed value did change, doctor names the inner path (`arguments<json>.invoice_id`) |
| the same text in another Unicode normalization form (NFC -> NFD) | `*` looks like a real change, named as such | only *if the form is not part of the contract*: `unicodedata.normalize("NFC", s)` before capturing |
| `0.0` -> `-0.0` | `~` sign of zero | `x + 0.0` before capturing |
| a changed content hash, a type change, a new key, anything else | `*` / `!` looks like a real change | nothing: review, then approve or fix |

Every suggested rule is checked to make both samples equal. It is withheld when it
would also match a stable value anywhere else in the capture, and for behaviors
captured with `scrub=False`. If a value marked as a real change changes again on a
re-run with no code edits, it is volatile: apply the conditional suggestion (sort,
round, normalize), or mask
it at capture time in that test.

## Semantic judge (v0.2) — gate nondeterministic AI text

Free-text AI output breaches the fingerprint gate on every rewording (measured:
25/25 false positives on real data). Mark such behaviors `semantic=True` and commit
the project's judge — the judge rules **equivalence only**; approval stays human:

```python
def test_summary(behavior):
    behavior("daily_summary", summarize(items), group="ai", semantic=True)
```

The flag is part of the approved behavior. Turning `semantic=True` on (or off) for
an approved behavior is itself a CHANGED (`semantic: False -> True`). The judge runs
only once a human has approved the behavior as semantic, so a one-word test edit can't
switch an exact behavior to lenient comparison.

The judge is a project decision, so it lives in your committed `pyproject.toml`.
`nightward run`, CI and the MCP agent all use it, and changing it shows up in a PR:

```toml
[tool.nightward]
judge = "anthropic:claude-haiku-4-5"   # real LLM (pip install "nightward[judge]")
# judge = "persona:editor"             # deterministic, key-free (see below)
```

The judge belongs to the project that owns the store: nightward reads the
`pyproject.toml` nearest the store directory (`.nightward`, or `--dir`), not the
path you run. `nightward run services/chat` and an agent's
`nightward_run(path="services/chat")` use the same judge as `nightward run .`, even
when `services/chat` has its own `pyproject.toml`. `nightward run` prints the file
it read (`judge: persona:editor (pyproject.toml)`). When approved `semantic=True`
behaviors changed and no judge is configured, `run` says so (`note: 1 approved
semantic behavior(s) compared exactly: no judge configured ...`), and the report,
`status --json` and MCP carry `"judge": {"spec": null, "unavailable": ...,
"compared_exactly": [...]}`. To try another judge for **one run**, override it.
The override is never remembered: the next plain `nightward run` and every MCP run
go back to the committed judge.

```bash
nightward run . --judge persona:strict                        # this run only
NIGHTWARD_JUDGE=persona:strict nightward run .                # same, via env (CLI only)
```

The `persona:*` judges are deterministic and need no key. Both judging personas
**fail closed**: a change to any digit or number, sign, currency or unit symbol,
operator (`+ - < > = !=` ...), emoji, negation, JSON key, or value type
(`49.99` vs `"49.99"`, a list vs a string) is DIFFERENT. Negation means English
negation words, and Korean, Japanese and Chinese words that carry a negation marker
(`않`, `없`, `못`, `불가`, `ない`, `ません`, `不`, `没`, `未` ...), since those languages
negate inside the verb. A word that merely contains such a marker (`少ない`) also
counts, which only makes lenient stricter.

Only natural-language **prose** is compared loosely. Everything else is compared
exactly:

- a string with no whitespace: ids, enums, currency and event codes, URLs
  (`acct_XyZwQ`, `usd`, `REFUND`, `charge.refunded`);
- code and markup: a string containing `= { } [ ] < > | ` * ` or an indented line
  (Python, YAML, Markdown tables);
- a string that is a JSON object or array, such as a tool call's `arguments`. It is
  compared as parsed JSON: keys, types and literals count, and JSON on one side only
  (`True` vs `true`) is a change.

Inside prose, line breaks count, and identifier-like words keep their case:
mixed case (`iPhone`), ALL CAPS (`USD`), or words joined by `_ . / - @ :`.
"Letter case" means a word's lower- and upper-case forms both match. Unicode case
folding is not used, so `Maßen`/`Massen` ("in moderation"/"in masses"), `ﬁ`/`fi` and
the KELVIN SIGN/`K` stay different. Japanese and Chinese are written without spaces,
so text with kana or Han characters counts as prose even without whitespace. Its
`。、，．；：！` marks count as sentence punctuation and full-width spaces as spaces;
every other character must match. A change in Unicode normalization only (NFC vs NFD,
e.g. Hangul from a macOS file name) is DIFFERENT. `review` and `doctor` name it
("same text in another Unicode normalization form (NFC -> NFD)") and suggest
`unicodedata.normalize("NFC", s)` before capturing.
In structured payloads only string values are compared loosely, and only when
they are prose.

| persona | rules prose SAME when... | use it for |
|---|---|---|
| `persona:editor` | only letter case, spaces within a line, or sentence punctuation (`. , ; : !` before a space or the end; `。、，．；：！` anywhere) differ. Every word must match; a unit after a number keeps its case (`5 mW` vs `5 MW`). | CI without a key: collapses cosmetic rewording only |
| `persona:lenient` | as editor, and ordinary words may also change (`went up` vs `rose`). Can pass `approved` vs `denied`. | tests and demos only, **never real gating** |
| `persona:strict` | never | forcing every mismatch to stay breached |

Any provider:model can plug in as a backend. Each ruling is recorded once per
fingerprint pair in `.nightward/judge/` (one JSON file per ruling) — a **committed
ledger**. Each entry records the behavior, model, verdict, reason, and the old and
new wording it ruled on (up to 1,000 chars each), so every ruling lands in the PR
diff. **Review ledger diffs like baseline diffs**: a ledger entry can turn a breach
green.

- A model judge (`anthropic:*`) rules once per pair. Its ruling is replayed from
  the ledger after that, so the model's own nondeterminism can't wobble the gate and
  fresh clones and CI replay verdicts without a key. A replayed ruling is marked
  `(replayed from the committed ledger, not ruled this run)` in `review`, and
  `"judge_replayed": true` in `status --json` and MCP: whoever last edited the
  ledger made that ruling.
- A persona is deterministic and free, so it rules again on every run and its
  ledger entries are a record only. A hand-edited persona entry (`DIFFERENT` ->
  `SAME`) never changes the verdict: `run` warns that the ledger entry did not match
  the persona's ruling and rewrites it, which shows up in `git diff`.

Rulings are visible wherever the verdict is:

- `nightward run` and `nightward status` count and name the behaviors ruled SAME.
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

Both return the `status --json` shape: `boundary`, `unapproved`, `changes` (`name`,
`kind`, `group`, plus `judged`, `judge_model`, `judge_reason` when a judge ruled),
`judged_same`, `stale`, `incomplete` (`{"failed": n, "errors": m}` or null),
`generated_at`, and `judge`. `boundary` is one of:

| `boundary` | meaning | what the agent should do |
|---|---|---|
| `intact` | no unapproved change | done |
| `breached` | unapproved changes | fix the code, or stop and ask a human to approve |
| `incomplete` | nothing unapproved, but capture tests failed or errored | fix the failing tests (see `incomplete` and `pytest_output_tail`) |
| `stale` | the baseline or capture moved since the report | call `nightward_run` again |
| `unknown` | no report yet | call `nightward_run` |

`nightward_run` adds `warnings`: `skipped`, `failed`, `errors`, `deselected`,
`xfailed`, `scrubbed` (values the default scrubbers masked), `scrub_unmatched`
(custom scrub rules that matched nothing), `pytest_returncode`, and
`pytest_output_tail` (pytest's last lines, so the agent can see why tests failed).
The agent is done when `boundary` is `"intact"` and `stale` is false.

Rules for the loop:

- **Call `nightward_run` after every code edit.** `nightward_status` and `gate` only
  report the last run. They don't notice code edited since then, and `stale` only
  covers a baseline that changed after the run.
- **The agent can't approve.** `approve` and `reject` are not exposed. If the agent
  that makes a change could also approve it, the gate would turn into a changelog.
  A human approves with the CLI and commits the baseline.
- **The judge is the humans' committed choice.** Behaviors approved as `semantic=True`
  are judged by `nightward mcp --judge <provider:model>` if the server was started
  with it, else by the committed `[tool.nightward] judge`. MCP ignores
  `$NIGHTWARD_JUDGE` and any `nightward run --judge` override, so a one-off demo run
  can't change the agent's gate. The tool has no judge argument, so the agent can't
  pick a lenient judge, and it gets the same verdict as a plain `nightward run`.

## Dashboard (`nightward view`)

![nightward dashboard — a breached boundary rendered from synthetic demo data](docs/assets/dashboard-light.png)

`nightward view` renders the blast radius as a self-contained static site — boundary
status, counts, and grouped diffs with copy-paste `approve`/`reject` commands. It is
**read-only** (decisions stay in the CLI) and **static** (no backend), so it also
deploys to GitHub Pages. Data is loaded via `fetch('./data.json')` and rendered with
`textContent` only — captured output never touches an HTML parser.

The copy-paste commands quote every behavior name for the shell picked in
"commands for:" (bash/zsh/sh, PowerShell, or cmd.exe; PowerShell is the default on
Windows). A name such as `x;touch${IFS}pwned` therefore arrives as one literal
argument and never runs as code. When a name has no safe form in the selected shell
(`%` or `!` in cmd.exe), the dashboard says so and offers no command.

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
   - `baseline/*.approved.json`, the judge ledger (`judge/`), scrub rules, and test
     files are code — review their diffs in every PR;
   - protect your main branch (required human review) and let CI re-run
     `nightward run && nightward gate` from source, so a locally forged
     "intact" can't merge itself;
   - never wire `approve` into the agent loop or CI (the MCP server
     deliberately doesn't expose it — keep your own glue to the same rule).
3. **A semantic judge can be wrong.** A false-SAME verdict is a hole in the
   gate. That's why judging is opt-in per behavior, the prompt is conservative
   (unsure → DIFFERENT), failures fall back to exact comparison, and every
   ruling is recorded in the committed judge ledger (`.nightward/judge/`) for
   human review. Persona rulings are recomputed every run, so editing their ledger
   entries changes nothing; a model's ruling is replayed from the ledger and marked
   as replayed.
   If a behavior must never be judged leniently, don't mark it `semantic=True`.
   Captured output is untrusted input to an LLM judge (it may quote retrieved
   documents or user text). The prompt inserts each output once, verbatim, in a
   block tagged with a fresh random id the text can't contain, and tells the model
   the blocks are data. That is a mitigation, not a guarantee, which is one more
   reason to review the rulings.
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
