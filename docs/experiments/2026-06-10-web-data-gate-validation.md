# Quantitative gate-quality validation: summary of the live web-data experiment

> **Date**: 2026-06-10
> **Spec**: follows the protocol in `docs/superpowers/specs/2026-06-09-nightward-live-email-ai-experiment-design.md`.
> Only the input source changed: Gmail → **real web-search data** (5 categories: tech / world news / finance / science / sports, 24 items + 3 market indices).
> Raw snapshots, captures, and the detailed RESULTS.md exist only in `live-experiment/` (gitignored). This document contains aggregate figures only.

## Verdict table

Results of running 58 behaviors (facts 8 · ai_text 25 · ai_struct 25) through the same pipeline:

| Layer | TP: injected regression | FP: unchanged re-run (A→A) | FP: AI drift (A→B) | Residual after taming | Verdict |
|---|---|---|---|---|---|
| `facts/*` deterministic aggregates | ✅ off-by-one → exactly 1 CHANGED | **0** | **0** | — | Clean gate |
| `ai_text` free text | — | 0 | **25/25** | 25 (irreducible) | Outside v0 scope |
| `ai_struct` structured | — | 0 | **4/25** | **0** (capture stable fields only) | Tameable |

## Conclusions (all spec §8 success criteria met)

1. **Gate validated**: in the deterministic layer, the injected regression was isolated and caught at the granularity of a single behavior (the blast radius breached only that group), with zero false positives on both re-runs and AI drift.
2. **Boundary demonstrated**: free-text AI output yields 100% false positives even on identical input — it cannot be gated with fingerprint equivalence. **Quantitatively confirms the motivation for v0.2 LLM-as-judge.**
3. **Practical compromise**: drift in structured AI output concentrates in borderline-judgment fields (priority/sentiment). Capturing only stable fields (topic, etc.) leaves 0 residual false positives — v0 user guidance: *structure your AI output, and leave the wobbly fields out of the capture.*

## Phase 4b — validating `nightward doctor` (a feature born from this experiment)

Validated `nightward doctor` (new) using this experiment's 4 ai_struct false positives as input:

- doctor pinpointed the drifting fields exactly: `priority` ×2, `sentiment` ×2. Free text (`ai_text`) was classified as a root (`$`) change, so **no field suggestion** (an honest report of the limit).
- Applied the suggested `scrub.register_field("priority")`/`("sentiment")` in conftest.py, re-baselined → re-ran RUN=b: **ai_struct residual false positives 4→0**, achieved without dropping the fields from the capture (unlike the STABLE_ONLY approach, the payload shape is preserved).
- The loop of finding a false positive → diagnosing → taming → re-validating closes with the CLI alone.

## Side findings

- The first capture tripped the no-spaces rule for behavior names (`validate_name`) — clear error message, and `run` printed the failed-test warning. Working as designed.
- Normalizing dates to `YYYY-MM-DD` resulted in zero scrubber misfires (confirms the spec §3 pitfall was avoided).
