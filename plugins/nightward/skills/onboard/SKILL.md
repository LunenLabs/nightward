---
name: onboard
description: Use when the user asks to put code under the nightward gate ("set up nightward for X", "onboard the payment module", "write captures for X"). Reads the code without changing it, writes a domain map that gives each rule's code and intent sources, writes capture tests from the map, runs the first capture, and hands the human the suspected defects and the review.
---

# Onboard code onto the nightward gate

The gate can only protect what is captured, and a first baseline is only worth
what the person approving it understood. Your job is to do the domain work, so
the human only has to make decisions: which scope, the answers to open
questions, which suspected defects are real, and what to approve.

## 0. Before you start

- nightward must be installed in the project environment (`nightward --version`).
  If there is no `.nightward/` store, run `nightward init`.
- Read `docs/payload-design.md` in the nightward docs if you haven't: payloads are
  tables a human can review.

## 1. Scope

If the user named the code, use it. Otherwise propose one to three candidates
where a silent change costs the most: money (prices, fees, settlements,
refunds), state machines (bookings, payments, orders), permissions, rankings.
Give one line on why for each, and **stop until the user picks**.

## 2. Analyse, read-only

**Do not edit any source file in this step**: no refactoring, and no fixing a bug
you find. The first capture must record what the code does today. A change made
before it lands in the baseline without anyone reviewing it, so the gate can never
show it.

Use Serena's symbol tools if they are available (`get_symbols_overview`,
`find_symbol`, `find_referencing_symbols`); otherwise Grep and Read. Collect:

- the entry points (service functions, handlers) and what they read and write
- states and transitions, and who may trigger each
- the inputs that branch: amounts and their boundaries, statuses, roles,
  missing or duplicate identifiers, rounding
- external calls (payment gateways, mail, clocks, randomness) that a capture must
  replace with a stub that records what was requested

For every rule, look for a second source that states the **intent**: a
docstring, a comment, a spec or doc, an existing test, an API schema.

- Code and intent agree: a rule.
- Code and intent disagree: a **suspected defect**. Note both sides with
  `file:line`.
- No intent anywhere: the rule is **unconfirmed**, because code alone would
  describe its own bugs as rules. Add it to the open questions.

## 3. Write the domain map

Write one file per group: `docs/nightward-domain/<group>.md`, or the folder in
`[tool.nightward] domain`. nightward reads only the front matter between the
`+++` lines. The body is for humans.

```markdown
+++
group = "pt.payment"
derived_from = "<git rev-parse --short HEAD>"

[rules.R1]
rule = "A late FAILED webhook never moves a paid payment back to pending"
code = "app/apps/pettracker/service.py:851"
intent = "docstring of handle_portone_webhook_event"

[rules.R2]
rule = "A payment is prepared only after the walker accepted the booking"
code = "app/apps/pettracker/service.py:672"
# no `intent`: unconfirmed, inferred from the code

[behaviors]
"pt.portone_webhook" = ["R1"]
"pt.payment_prepare" = ["R2"]
+++

# Payment

## States and inputs covered
(the matrix the captures walk: each state x each event, each boundary)

## Suspected defects
- R1: code sets `pending` on FAILED for a paid payment (service.py:851), the
  docstring says FAILED only applies before payment.

## Open questions
- R2: is paying before acceptance intended? Nothing states it.
```

Rule ids are local to a map. Every behavior you capture in step 4 gets a
`[behaviors]` entry.

## 4. Write the captures

Put them in `tests/nightward/test_<group>.py`. Capture what the code returns and
never write expected values. Use the `behavior` fixture or `@capture`.

- One behavior per rule cluster. The payload is a table from input to outcome,
  for example one key per state x event.
- Record an error as an outcome: its type, status code and message.
- Leave out ids, timestamps, random values and machine paths. Use fixed inputs
  and stub the clock.
- Stub external calls and record what they were asked to do.

If `[tool.nightward] path` isn't set in `pyproject.toml`, tell the human to set it
to the capture folder, so the agent loop runs these tests and not the whole suite.

## 5. Run twice

Run `nightward run`, copy `.nightward/pending/` aside, then run it again. Every
file must be byte-identical. If one isn't, a payload holds a volatile value: take
it out of the payload, or normalize it if it is truly volatile
(`docs/normalization.md`). A gate that flags noise is a gate people stop reading.

## 6. Hand over, in one message

- the scope and the map files
- the behaviors per group
- the **suspected defects**, each with code vs intent and `file:line`
- the **open questions**
- a suggested review depth per group (`docs/review-depth.md`), for example money
  `exhaustive`. Suggest only: the depth is the humans' setting.
- what they do next: run `nightward review`, approve the behaviors that are
  right, and reject the ones that capture a defect.

## 7. After the human decides

Fix only the defects the human confirmed. Run `nightward run` after each fix. A
rejected behavior comes back with its new value for the human to review again.
Every approved behavior must stay unchanged: if a fix moves one, the gate shows it,
and you report it. That report is the gate doing its job, not noise to explain
away.

## Never

- Edit source code before the first capture.
- Approve or reject, edit anything under `.nightward/`, or write a review depth
  into `pyproject.toml`.
- Mark a rule as confirmed without an intent source you can point to.
- Hard-code a captured value into a test.
