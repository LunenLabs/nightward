# Designing payloads a human can review

[nightward](../README.md) guide. How to shape what you pass to `behavior()` so that
every change it reports is one a reviewer can read and decide on.

The gate is only as good as the review behind each approval. A payload that turns
one cause into hundreds of diff lines makes the reviewer skim, and an approval
nobody read is the same as an automatic one. These rules came out of real
first reviews that ran past a thousand diff lines; follow them and the first
review is a page, not a scroll.

`nightward review --summary` condenses whatever you captured (every changed path,
with its old -> new values counted), but a well-shaped payload needs no condensing.

## 1. Aggregate by cause, keep the members

Capture *what happened to which items*, not every item's raw record. One broken
import should read as one change, not as 80.

```python
# Avoid: one record per case - a single ImportError rewrites all 80 entries
behavior("eval_cases", [{"id": c.id, "input": c.input, "output": run(c)} for c in CASES])

# Prefer: outcome -> the cases that had it (sorted, so the order is stable)
outcomes: dict[str, list[str]] = {}
for c in CASES:
    outcomes.setdefault(classify(run(c)), []).append(c.id)
behavior("eval_outcomes", {k: sorted(v) for k, v in sorted(outcomes.items())})
```

```
- "pass":             ["c01", "c02", ... "c80"]
+ "ERR ImportError":  ["c01", "c02", ... "c80"]
```

Keep the member lists, not just counts. `{"pass": 70, "fail": 10}` hides a swap:
case A starts failing while case B starts passing, the counts stay the same, and
the gate stays green. `{"fail": ["c07", ...]}` shows exactly which cases moved.

Capture the detail you need to fix a failure in a *separate* behavior (or not at
all), so the summary behavior stays short and the detailed one is reviewed only
when it moves.

## 2. Record each input once

When several behaviors embed the same input (a shared fixture, the request that
produced every response), one edit to that input changes all of them, and the
reviewer reads the same diff N times. Capture shared inputs in one behavior and
refer to them by a stable id elsewhere.

```python
# Avoid: the 2 KB prompt is copied into every case
behavior("answers", [{"prompt": PROMPT, "question": q, "answer": ask(q)} for q in QS])

# Prefer: the prompt once, the cases by id
behavior("prompt", PROMPT, group="ai")
behavior("answers", {q.id: ask(q) for q in QS}, group="ai")
```

## 3. Structured fields over free text

Free text changes on every rewording, and nobody can tell a regression from a
paraphrase in a wall of `-`/`+` lines. In our own experiment (AI summaries of 30
frozen emails, summarized twice, no code change) all 31 free-text behaviors
breached; captured as structured fields (category, priority, intent), 2 of 31 moved.

```python
# Avoid
behavior("triage", llm_triage(ticket))            # "This looks like a billing issue..."

# Prefer: the decision as fields, the prose (if at all) as a semantic behavior
t = llm_triage_structured(ticket)
behavior("triage", {"category": t.category, "priority": t.priority, "refund": t.refund})
behavior("triage_text", t.explanation, semantic=True)   # see semantic-judge.md
```

## 4. Leave the machine out of the payload

Values that depend on where the test ran break the gate the moment it runs
somewhere else (a teammate's Mac, a Linux CI runner after a Windows baseline):

| Captured                         | Differs by           | Capture instead                         |
|----------------------------------|----------------------|-----------------------------------------|
| `str(path)`                      | OS, checkout dir     | `path.relative_to(root).as_posix()`     |
| `str(exc)` of an `OSError`       | OS, locale           | `type(exc).__name__` (+ `exc.errno`)    |
| raw text read from files         | `\r\n` vs `\n`       | `text.splitlines()`                     |
| `repr(float)` after heavy math   | CPU, BLAS, Python    | `round(x, 6)`                           |
| `sys.version`, `os.sep`, hostnames | everything         | nothing - it is not your behavior       |

nightward records the OS and Python version a behavior was captured on. When a
change shows up on a different OS or Python than the one its baseline was
approved on, `run`, `review`, `gate` and `status --json` (`env_mismatch`) say
so - suspect the environment before the code.

## 5. Make order and identity deterministic

Sort anything whose order is not the contract (`sorted(set_)`, `sorted(rows, key=...)`),
use stable ids instead of positions or generated ids, and leave timestamps and
UUIDs to the default scrubbers ([what nightward normalizes](normalization.md)).
If order *is* the contract (a ranking, an event log), keep it - and keep the list
short enough that a reorder is readable.

## 6. Keep one behavior to one screen

If `nightward review NAME` scrolls, split the behavior along the lines a reviewer
thinks in (one per feature or `group`), or aggregate further. Many small
behaviors give a blast radius that says *where* something moved; one huge
behavior only says *that* it did.
