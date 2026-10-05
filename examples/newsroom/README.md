# newsroom — gating nondeterministic AI text (v0.2 semantic judge)

A two-layer system: deterministic **facts** (section counts, top story) and an
AI-style **daily brief** whose wording drifts between runs even when nothing
meaningful changed. The exact fingerprint gate is perfect for the first layer
and measurably wrong for the second — free text re-breaches on every rewording.
`semantic=True` + a judge model closes that gap without opening the gate.

## Run it

```bash
cd examples/newsroom

# 1. the approved baseline is committed. Confirm intact:
nightward run .                       # boundary intact

# 2. simulate LLM drift: same facts, different wording
NEWSROOM_REWRITE=1 nightward run .    # boundary BREACHED — a false positive (v0 limit)

# 3. try judges, one run at a time — any provider:model; personas need no API key
NEWSROOM_REWRITE=1 nightward run . --judge persona:editor    # still breached (conservative)
NEWSROOM_REWRITE=1 nightward run . --judge persona:lenient   # intact — 1 ruled semantically SAME
nightward gate                                               # exit 0

# 4. --judge is a one-run override, never remembered:
NEWSROOM_REWRITE=1 nightward run .                           # breached again (no judge)

# with a real model (pip install "nightward[judge]", ANTHROPIC_API_KEY set):
NEWSROOM_REWRITE=1 nightward run . --judge anthropic:claude-haiku-4-5
```

In a real project you commit the judge instead of typing it, so CI, every
teammate and the MCP agent use the same one (`persona:lenient` is for this demo
only, never for real gating):

```toml
# pyproject.toml
[tool.nightward]
judge = "anthropic:claude-haiku-4-5"
```

## What to notice

- Only `daily_brief` carries `semantic=True` (see `test_newsroom.py`); the
  `facts` group never reaches the judge — deterministic output stays exact.
- Every ruling is audited: the CLI prints `judged DIFFERENT by <model>` /
  `ruled semantically SAME`, the dashboard shows an `AI-judged` badge with the
  verdict reason, and `report.json` carries `judged` fields.
- Verdicts land in `.nightward/judge_verdicts.json` — a **committed** ledger,
  so re-runs, `approve` recomputes, fresh clones, and CI never re-ask the model,
  and every ruling shows up in the PR diff for human review.
- The judge rules **equivalence only**. Promoting a change into the baseline
  is still a human `nightward approve` — a judge cannot let an agent
  self-approve its own changes.
