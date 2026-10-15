# Capture coverage: showing what the gate can't see

Status: **proposal, not decided.** Pick an option before any code.

## Problem

The README's problem statement is "an agent fixes A and moves B, C and D, which no
test pins down." nightward only sees what `behavior()` captures, so B, C and D
are caught only if someone already wrote a capture for them. nightward removes
the cost of writing expected values, not the question of *what* to capture. If
the agent writes the capture tests, the agent also decides what the gate watches.

Today nothing tells a user how much of their code the captures exercise. A green
gate over three captures of a 40-module app looks exactly like a green gate over
three hundred.

## Constraints

- **A noisy gate is a dead gate.** Anything that adds captures automatically adds
  noise and payloads nobody designed for review
  ([payload design](../../payload-design.md)).
- **Approval stays human.** Whatever widens coverage must reach the baseline
  through `review` and `approve`, never on its own.
- Scope: pytest only, small surface (CONTRIBUTING).

## Options

### A. Report the gap (`nightward coverage`)

Run the capture tests under `coverage.py` (or `sys.monitoring` on 3.12+) and
report, per module and function, whether any capture test executed it. Print the
uncovered public functions; `status --json` and the dashboard carry a summary
(`"capture_coverage": {"functions": 0.34, "uncovered": [...]}`).

- \+ No new captures, so no new noise. Honest: the user sees the blind spot.
- \+ Gives an agent a concrete, reviewable to-do ("these 12 functions have no
  capture"), while a human still owns which captures get approved.
- − Executed is not observed: a function can run in a capture test whose payload
  ignores its result. Must be labelled "executed by capture tests", not
  "protected".
- − Adds a `coverage` dependency (optional extra) and a slower run.

### B. Watch functions (`nightward.watch("app.pricing.checkout_total")`)

Registered in a `conftest.py`, it wraps a product function and records each
call's arguments and return value as behaviors, named by test and call index.
This is the `@capture` decorator idea from the 0.3.3 feedback round, which was
deferred.

- \+ One line covers every existing test that calls the function.
- − Payload volume and name stability: a refactor of a test reorders calls and
  every name moves; arguments are often not JSON.
- − Most likely to produce the noise that kills the gate. Needs per-function
  opt-in, argument filters and call aggregation before it's usable.

### C. Suggest captures (`nightward suggest`)

List public functions without a capture (from A's data) and print a starter
`behavior(...)` test for each, for a human or an agent to edit.

- \+ Cheap once A exists. Keeps humans in the loop: the test is a reviewed diff.
- − A generator, which nightward says it isn't; keep it to scaffolding.

## Recommendation

**A first, then decide on C from real use; leave B deferred.** A is the only
option that adds information without adding noise, and it turns "intact" into
"intact over N% of the code your captures execute", which is the honest claim.
It also measures whether B is worth its cost: if real users show large
uncovered surfaces that captures can't reasonably reach, B becomes the next
question.

## Open questions

1. Unit of coverage: functions (readable) or lines (precise)? Proposal: public
   functions, with lines available in `--json`.
2. Does coverage belong in the gate verdict? Proposal: **no**, it's information.
   A threshold would be a policy engine, which the scope excludes.
3. Where it shows: `run` summary line, `status --json`, dashboard panel.
