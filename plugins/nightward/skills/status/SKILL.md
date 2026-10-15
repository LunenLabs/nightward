---
name: status
description: Show the nightward gate verdict for this repository and what a human needs to review.
disable-model-invocation: true
---

Run `nightward status --json` in the repository root and report, briefly:

1. The `boundary` and what it means (intact, breached, incomplete, stale,
   partial or unknown), and `generated_at` (the verdict is from the last run;
   code edited since then isn't reflected).
2. Each unapproved change: kind (NEW, CHANGED, REMOVED), name and group.
3. What the human runs next, in a code block:
   - nothing is reported or the report is stale: `nightward run .`
   - breached: `nightward review`, then approve the intended changes by name,
     and fix the code for the rest.

Do not run approve or reject yourself; those are the human's commands.
