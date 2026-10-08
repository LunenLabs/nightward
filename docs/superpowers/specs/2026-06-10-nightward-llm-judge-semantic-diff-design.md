# nightward v0.2: LLM-as-judge semantic diff design

> **Date**: 2026-06-10
> **Status**: Implemented (2026-06-10) — added `judge.py` + the keyless `persona:*` backend; multiple models are
> selected via a `provider:model` spec. Acceptance verified by `tests/test_judge.py` + a real-data A/B.
> **Motivation (quantitative)**: `docs/experiments/2026-06-10-web-data-gate-validation.md` — free-text AI output
> produces fingerprint false positives **25/25 (100%)** even with identical input and identical code. field-scrub
> (`doctor`) tamed the structured layer down to 0 residual, but free text is in principle ungateable with the v0
> equivalence oracle.

## 1. Purpose

For non-deterministic *text* behaviors only, allow the equivalence oracle to be swapped from
`sha256(canonical_json)` → **"do they mean the same thing?"**. Never touch the deterministic layer (for both cost and
trust reasons).

## 2. Gate principles (non-negotiable — CLAUDE.md §scope guardrails)

- **The judge only decides equivalence; it never approves.** A SAME verdict folds the change into "not a change"; it
  does not modify the baseline. Changing the baseline is still exclusive to the human CLI `approve`.
- **Explicit opt-in.** Only behaviors marked with `behavior(name, val, group=, semantic=True)` take the judge path.
  The default is the v0 fingerprint — as the experiment showed, the deterministic layer is already clean.
- **Conservative fallback when the judge is unavailable.** No API key / network failure / unparseable response →
  fall back to fingerprint comparison (= fail toward breach). The gate would rather close loudly than open silently.

## 3. Architecture

```
core/diff.py compare()
  └ fingerprint mismatch & behavior.semantic=True
        └ judge.equivalent(old_text, new_text)   # new judge.py
             ├ ledger hit (.nightward/judge_verdicts.json — committed, key=old_fp:new_fp:spec) → no re-judging
             ├ SAME      → Change(kind=UNCHANGED, judged=True)  # audit mark in the report
             └ DIFFERENT → Change(kind=CHANGED,  judged=True)
```

- **Determinism**: the same (old_fp, new_fp) pair is judged exactly once via the cache — so the judge's own
  non-determinism cannot shake the gate. The ledger is **committed** — verdicts replay deterministically on a fresh
  clone/CI, and humans review them in the PR diff (corrected in the 2026-06-10 critique round, which caught the
  contradiction of a transient cache being ephemeral).
- **Prompt**: fixed conservative criterion — "Is this a rephrasing with identical facts, figures, and conclusions? If
  unsure, DIFFERENT." temperature 0, structured output (JSON `{verdict, reason}`).
- **Model**: defaults to the latest small model (cost), swappable via `--judge-model`. Optional extra `[judge]`
  (`anthropic` SDK), same pattern as `mcp` — the judge function is injected so core tests run without the SDK.

## 4. Surface changes

- `pytest_plugin`: `behavior(..., semantic=True)` → add a `semantic: bool = False` field to Behavior
  (schema backward compatible: existing approved files with no semantic = False).
- `run`/`review`/`view`/`status --json`: mark judged changes with `judged: true` (audit visibility).
- MCP surface unchanged — the judge is internal to run; nothing new exposed in `_TOOLS`.

## 5. Risks and limits (to be documented)

- A judge misjudgment (SAME, but actually a regression) = **a hole in the gate**. Hence off by default, opt-in,
  conservative prompt, and an audit record of verdicts. "When in doubt, breach" always wins.
- Cost: number of CHANGED · semantic behaviors × 1 call (0 after caching). Per the experiment, one run B = at most
  25 calls.
- The experiment's `ai_run_a/b` data becomes the acceptance fixture as-is: for A→B with semantic=True, false
  positives must converge from 25→0, and B' with injected factual distortions must be caught as DIFFERENT.

## 6. Scope (YAGNI)

- **In**: semantic flag, judge.py (+cache), conservative fallback, audit mark in the report, acceptance fixture.
- **Out**: automatic approval (permanently OUT), applying the judge to structured/deterministic payloads by default,
  multi-provider abstraction, generating natural-language explanations of semantic diffs.
