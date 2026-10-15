# Contributing to nightward

Thanks for helping. nightward stays small on purpose — the value is a tight,
trustworthy core, not features.

## Dev setup

```bash
python -m venv .venv
.venv/Scripts/activate      # Windows
# source .venv/bin/activate # macOS/Linux
pip install -e ".[dev]"
```

## Before you push

```bash
pytest -q -n auto  # all tests must pass (parallel: ~5 min instead of ~15)
ruff check .       # lint must be clean
```

Tests live in `tests/<area>/`: `core`, `gate`, `judge`, `doctor`, `scrub`, `cli`,
`mcp`, `view`, `adapters` and `release`. Put a new test next to the area it
exercises, not in a file named after the review that found it.

## Scope guardrails

In scope: the capture → blast-radius → approve → gate → loop-signal pipeline,
robustness, pytest integration, input adapters, and the semantic judge.

Out of scope for now (deliberately): web UI, multi-language runners. These are
tracked as future work — open an issue before building them.

LLM-as-judge semantic diffing shipped in v0.2 (`--judge provider:model`). New
judge backends are welcome — open an issue first.

## Principles

- The approved baseline is the only source of truth. Keep it human-diffable.
- Never let false positives creep in — a noisy gate is a dead gate.
- Errors users can cause should be `NightwardError` with a clear message, never a
  traceback.
