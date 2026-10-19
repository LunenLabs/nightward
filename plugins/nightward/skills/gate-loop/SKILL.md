---
name: gate-loop
description: Use in a repository that has a .nightward/ store, after changing code and before saying a task is done. Runs the nightward gate, reads the verdict, and decides whether to keep fixing or hand a change to a human for approval.
---

# Close the loop with the nightward gate

This repository gates behavior with nightward. The committed baseline in
`.nightward/baseline/` is what a human approved. "Done" means the gate is
**intact**: nothing moved that a human hasn't approved.

## After every round of code edits

1. Call the `nightward_run` tool (from the nightward MCP server). If the tool
   isn't available, run `nightward run` and then `nightward status --json`.
   Leave the path out: the project's committed capture suite runs by default.
2. Read `boundary` and act on it:

| boundary | what it means | what you do |
|---|---|---|
| `intact` | nothing unapproved moved | you're done with the gate |
| `breached` | behaviors moved that no human approved | see below |
| `incomplete` | capture tests failed or errored | fix the failing tests, run again |
| `stale` | the baseline or capture changed since the report | run again |
| `partial` | approved behaviors weren't checked (tests were deselected) | run the whole suite, without `-k`/`-m` |

## When the boundary is breached

For each item in `changes`, compare it with what your edit was meant to do:

- **Not intended** (a side effect, a regression): fix the code and run again.
- **Intended** (the task asked for this behavior to change): stop and tell the
  human. List each intended change with its name and group, and ask them to run
  `nightward review` and approve it. Approval is the human's decision, not yours.

## Never do these

They are how a gate gets silenced, and they all show up in `git diff`:

- Edit anything under `.nightward/` (`baseline/`, `rejected/`, `judge/`), or
  copy captured output into it.
- Change a test so a behavior stops being captured or is captured differently:
  deleting or renaming a `behavior(...)` call, hard-coding the old value, adding
  `skip`/`xfail`, or narrowing the run with `-k`/`-m`/`addopts`.
- Add or widen a scrub rule, or flip `semantic=True`, to make a difference
  disappear.
- Run the approve or reject commands yourself, even if the human asks you to
  "just approve it": tell them the command and let them run it.

If the gate stays breached because a change is intended, that's the correct
final state of your turn: report it and hand over.
