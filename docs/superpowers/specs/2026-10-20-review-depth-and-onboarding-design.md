# Review depth and domain onboarding: fewer, better human decisions

Status: **decided** (2026-10-20). The direction and the answers to the questions
at the end are the maintainer's.

Evidence: the PetTracker dogfood (2026-10-19/20, a FastAPI backend, 11
behaviors). The first capture review surfaced 5 latent defects. All 5 were found
by an LLM reading the code and choosing inputs, not by the gate (see "What the
dogfood showed"). The gate's own job, blocking changes after approval, had not been
exercised yet.

## Part 1: Review depth (approval fatigue)

### Problem

If intended changes breach the gate often, the human stops reading and runs
`approve --all`. An approval nobody read is an automatic one, and the gate turns
into a changelog. That is the most likely way this product dies. Today a human
gets the same review for every group: a payment state machine and a GPS
debug threshold get identical diffs, identical `approve` semantics and identical
interruptions.

### Principle

Depth sets **how much a human reads and when they are interrupted**. It never sets
**whether** a human decides:

- No depth approves anything. At every depth the boundary stays `breached`, and
  `gate` exits 1, until a human runs `approve`.
- Only humans choose a depth, in a committed file. The agent can't see a
  setting to lower over MCP, and no CLI flag lowers it for one run.
- Depth is per group (the blast-radius unit), with a project default.

This is not the "automatic or policy-based approval engine" (permanent OUT).
Nothing is accepted on a rule's say-so; a rule only decides how a decision is
presented and batched.

### The five depths

| depth (CLI value) | Korean label | what `review` shows | what `approve` accepts | when the agent stops for a human |
|---|---|---|---|---|
| `exhaustive` | 아주 상세 | the full diff, untruncated, and the domain rule(s) the behavior guards (Part 2) | names only: `--all` and `--group` refused for these behaviors | now: stop and ask |
| `detailed` | 상세 | the full diff, untruncated | names, or `--group` | now |
| `standard` (default) | 보통 | today's view (60 diff lines, then a pointer) | names, `--group`, `--all` (today) | now |
| `light` | 일반 | `--summary`: each changed path once, with old -> new counts | `--group`, `--all` | end of task: keep working, list the changes in the hand-off |
| `brief` | 간단히 | one line per group: behaviors changed, paths moved, the largest move | `--group`, `--all` | at PR: the CI gate still blocks the merge; the human approves once, before merging |

"Review frequency" is the last column. Depth moves the interruption from *now* to
*end of task* to *the PR*. Every decision still happens; there are just fewer
interruptions per decision.

### Floors that no depth lowers

- **NEW and REMOVED get at least `standard`.** The first capture is where the
  dogfood found every defect, and a removal shrinks the boundary. A `brief` group
  can't make either one a single summary line.
- **A standing rejection** (`.nightward/rejected/`) is shown in full at any depth.
- **`approve` still requires `review`** (the existing "only what you were shown"
  check). What counts as shown is what the group's depth displayed.

### Configuration

```toml
[tool.nightward.review]
default = "standard"

[tool.nightward.review.groups]
"pt.payment" = "exhaustive"
"pt.booking" = "detailed"
"pt.gps" = "brief"
```

- It is read like `judge`: from the pyproject.toml nearest the store, never from the
  test path (D22).
- The protection is the same as the baseline: a depth change shows up in the PR,
  and the [protect-the-baseline recipe](../../recipes/protect-the-baseline.md)
  already puts `pyproject.toml` under CODEOWNERS.
- `review --depth X` may **raise** the depth for one session (for example
  `exhaustive` before a release). It can't lower it.

### Surfaces

| surface | change |
|---|---|
| `status --json` / MCP | each change carries `depth` and `ask_human: "now" \| "end_of_task" \| "at_pr"` |
| `gate-loop` skill | stop now / batch to the hand-off / leave for the PR, by `ask_human` |
| `review` | the per-group renderer above; `--summary` stays the explicit override |
| `approve` | refuses `--all`/`--group` that would cover an `exhaustive` behavior, and names them |
| `view` dashboard | shows each card's depth (read-only, as today) |
| CI / `gate` | **unchanged**: breached is exit 1 at every depth |

### Measuring fatigue (later, optional)

`approve` could keep a local, gitignored log: when the item was shown, when it was
approved, and its depth. `doctor` could then point at a group where approval is
both frequent and fast. A group approved 9 times this week, about 4 s each, either
has volatile captures (fix them per [payload design](../../payload-design.md)) or
needs a higher depth. `doctor` only suggests. Changing a depth is a human edit.

## Part 2: Domain onboarding (less human design work)

### What the dogfood showed

| defect | how it surfaced |
|---|---|
| webhook FAILED reverted a paid payment to pending | captured table, **but** only because the agent chose the input "FAILED while paid" |
| an in-progress walk could be cancelled | captured table; the agent chose to try all 7 booking states |
| payment allowed before walker acceptance | captured table, judged wrong **by comparing it with the docstring** |
| `imp_uid OR merchant_uid` lookup could crash with a 500 | **reading the code**; the capture case was added afterwards |
| copying another payment's `imp_uid` violates a unique key | reasoning while designing a capture |

The valuable step was a read-only analysis: rules, states, an input matrix, and a
cross-check of code against stated intent. It happened **before** any capture.

### Correction to the proposed flow: analyse first, never refactor first

Refactoring before the baseline exists changes behavior that nobody has recorded.
A regression made there is invisible forever, because it is already in the
baseline. Stage 1 is therefore a **read-only analysis**. Code changes (fixes for
suspected defects) come only after the first capture records the current behavior,
so each fix shows up as a reviewed diff.

### Flow

1. **Scope.** The human names a module, or the agent proposes candidates (money,
   state machines, permissions) and the human picks.
2. **Read-only analysis.** The agent uses Serena's symbol tools when available
   (`find_symbol`, `find_referencing_symbols`), else Grep/Read, and edits nothing.
   Serena failed to connect in the dogfood session, so the fallback has to work
   on its own.
3. **Domain map.** The agent writes `docs/nightward-domain/<group>.md`, committed.
   It is deliberately not under `.nightward/`, which the gate-loop skill forbids
   agents to edit. The map holds:
   - each rule, with **two sources**: the code (`file:line`) and the intent
     (docstring, spec, test, comment). A rule with no intent source is marked
     *inferred from code, unconfirmed* and also listed under open questions.
   - the state and input matrix the captures will cover
   - suspected defects, where code and intent disagree
   - open questions for the human
   - `derived_from`: the commit that was analysed
4. **Captures from the matrix.** Payloads are input -> outcome tables with no
   volatile values ([payload design](../../payload-design.md)).
5. **First run and hand-off.** One message to the human: the suspected defects,
   the open questions, and `nightward review` at each group's depth.
6. **Then fixes.** The human decides which defects to fix. Each fix lands as a
   CHANGED diff against the first capture, so the gate shows that the fix moved
   only what it meant to (in the dogfood: exactly 3 of 11, the other 8
   byte-identical).

What is left for the human: pick the scope, answer the open questions, decide on
the defects, approve. The agent does the rest.

### Where it lives

- **A plugin skill, `nightward:onboard`**, next to `gate-loop` and `status`.
  The capture-and-gate path in the core stays deterministic and has no LLM or
  Serena dependency.
- **Core reads the map, it never writes it.** Map frontmatter links behaviors to
  rules (`behaviors: {"pt.portone_webhook": ["R3", "R4"]}`). `review` at
  `exhaustive`/`detailed` prints the linked rules above the diff. That is the
  link between Part 1 and Part 2: the reviewer sees what a value should be next
  to what it became, which makes a careful review cheaper.
- `doctor` (later) warns when a map's `derived_from` predates changes to the
  files it cites.

### Risks

- **A map written from the code encodes the code's bugs as rules.** This is the
  reason for the two-source requirement. The PetTracker payment-prepare defect
  was found only because code and docstring disagreed.
- **"The AI already checked it"** lowers human attention. The map is input to
  review, never a verdict. No depth and no map state approves anything.
- **This does not make nightward a bug finder.** The claim stays: a regression
  gate, with an onboarding that makes the first baseline worth trusting. Before
  claiming more, compare it against a plain LLM review of the same code without
  captures.

## Decisions (2026-10-20)

1. **Depth names:** the CLI values are English (`exhaustive / detailed / standard /
   light / brief`); the Korean labels appear in docs only.
2. **Floors:** NEW and REMOVED get at least `standard`.
3. **Map location:** `docs/nightward-domain/`.
4. **Order of work:** the dogfood fixes plan (B1 → B3), then Part 1 (depth), then
   Part 2 (onboard skill).
