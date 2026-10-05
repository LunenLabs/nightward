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

# 1. capture current behavior and approve it as the baseline
nightward run example
nightward approve --all

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
nightward run     re-run tests → capture → compute blast radius
nightward review  show changed behaviors with diffs
nightward doctor  explain what moved in CHANGED behaviors; suggest scrub rules only
                  for values that are volatile by evidence (see below)
nightward approve promote pending behavior(s) into the baseline
                  (--all takes NEW/CHANGED; REMOVED needs a name or --include-removed)
nightward reject  confirm a change as a real regression (boundary stays breached)
nightward gate    exit 0/1 for CI and agent loops (1 also if the report is stale)
nightward status  machine-readable boundary signal (--json)
nightward view    build a static, read-only dashboard and view it in a browser
```

A skipped test or a partial path (`nightward run tests/test_a.py`) captures nothing for
the behaviors it didn't reach, so they read as REMOVED. That is why `approve --all`
leaves removals alone, and `--include-removed` refuses after a run with skipped or
failed tests. Capture runs in a single process: `nightward run` forces `-n 0` if
pytest-xdist is installed, and `--nightward-record` with `-n` is a usage error.

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
for human review. A judge that can't rule (SDK not installed, no
`ANTHROPIC_API_KEY`, API error, unparseable reply) falls back to the exact
comparison, so the gate fails closed, and says so: `nightward run` prints
`warning: judge <spec> unavailable (<reason>); N semantic behavior(s) compared
exactly`, and the report and `status --json` carry
`"judge": {"spec", "unavailable", "compared_exactly"}`. Rulings are also
surfaced in the CLI, `status --json`, and the dashboard.

## Dashboard (`nightward view`)

![nightward dashboard — a breached boundary rendered from synthetic demo data](docs/assets/dashboard-light.png)

`nightward view` renders the blast radius as a self-contained static site — boundary
status, counts, and grouped diffs with copy-paste `approve`/`reject` commands. It is
**read-only** (decisions stay in the CLI) and **static** (no backend), so it also
deploys to GitHub Pages. Data is loaded via `fetch('./data.json')` and rendered with
`textContent` only — captured output never touches an HTML parser.

> ⚠️ The dashboard embeds your captured behaviors. **Do not publish a real `.nightward/`
> store to a public site.** The Pages workflow only publishes synthetic clean-room data
> (`scripts/build_demo.py`).

## Threat model — what this gate does and does not protect against

Be precise about the guarantee: **"boundary intact" means no *captured* behavior
changed** — nothing more. Read these three limits before trusting the green light:

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
static dashboard, LLM-as-judge semantic diff (v0.2, multi-model).
Out (v1): PR-comment summaries, call-graph grouping, multi-language.
