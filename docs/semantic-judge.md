# Semantic judge: gate nondeterministic AI text

[nightward](../README.md) reference. How `semantic=True` behaviors are judged, what each persona tolerates, and how the committed judge ledger works.


Free-text AI output breaches the fingerprint gate on every rewording (measured:
25/25 false positives on real data). Mark such behaviors `semantic=True` and commit
the project's judge — the judge rules **equivalence only**; approval stays human:

```python
def test_summary(behavior):
    behavior("daily_summary", summarize(items), group="ai", semantic=True)
```

The flag is part of the approved behavior. Turning `semantic=True` on (or off) for
an approved behavior is itself a CHANGED (`semantic: False -> True`). The judge runs
only once a human has approved the behavior as semantic, so a one-word test edit can't
switch an exact behavior to lenient comparison.

The judge is a project decision, so it lives in your committed `pyproject.toml`.
`nightward run`, CI and the MCP agent all use it, and changing it shows up in a PR:

```toml
[tool.nightward]
judge = "anthropic:claude-haiku-5-5"   # real LLM (pip install "nightward[judge]")
# judge = "persona:editor"             # deterministic, key-free (see below)
```

The judge belongs to the project that owns the store: nightward reads the
`pyproject.toml` nearest the store directory (`.nightward`, or `--dir`), not the
path you run. `nightward run services/chat` and an agent's
`nightward_run(path="services/chat")` use the same judge as `nightward run .`, even
when `services/chat` has its own `pyproject.toml`. `nightward run` prints the file
it read (`judge: persona:editor (pyproject.toml)`). When approved `semantic=True`
behaviors changed and no judge is configured, `run` says so (`note: 1 approved
semantic behavior(s) compared exactly: no judge configured ...`), and the report,
`status --json` and MCP carry `"judge": {"spec": null, "unavailable": ...,
"compared_exactly": [...]}`. To try another judge for **one run**, override it.
The override is never remembered: the next plain `nightward run` and every MCP run
go back to the committed judge.

```bash
nightward run . --judge persona:strict                        # this run only
NIGHTWARD_JUDGE=persona:strict nightward run .                # same, via env (CLI only)
```

The `persona:*` judges are deterministic and need no key. Both judging personas
**fail closed**: a change to any digit or number, sign, currency or unit symbol,
operator (`+ - < > = !=` ...), emoji, negation, JSON key, or value type
(`49.99` vs `"49.99"`, a list vs a string) is DIFFERENT. Negation means English
negation words, and Korean, Japanese and Chinese words that carry a negation marker
(`않`, `없`, `못`, `불가`, `ない`, `ません`, `不`, `没`, `未` ...), since those languages
negate inside the verb. A word that merely contains such a marker (`少ない`) also
counts, which only makes lenient stricter.

Only natural-language **prose** is compared loosely. Everything else is compared
exactly:

- a string with no whitespace: ids, enums, currency and event codes, URLs
  (`acct_XyZwQ`, `usd`, `REFUND`, `charge.refunded`);
- code and markup: a string containing `= { } [ ] < > | ` * ` or an indented line
  (Python, YAML, Markdown tables);
- a string that is a JSON object or array, such as a tool call's `arguments`. It is
  compared as parsed JSON: keys, types and literals count, and JSON on one side only
  (`True` vs `true`) is a change.

Inside prose, line breaks count, and identifier-like words keep their case:
mixed case (`iPhone`), ALL CAPS (`USD`), or words joined by `_ . / - @ :`.
"Letter case" means a word's lower- and upper-case forms both match. Unicode case
folding is not used, so `Maßen`/`Massen` ("in moderation"/"in masses"), `ﬁ`/`fi` and
the KELVIN SIGN/`K` stay different. Japanese and Chinese are written without spaces,
so text with kana or Han characters counts as prose even without whitespace. Its
`。、，．；：！` marks count as sentence punctuation and full-width spaces as spaces;
every other character must match. Everything between two digits that is not a
letter (marks, dashes, slashes and spaces of any script and width, including thin
and no-break spaces) is part of the number and compared exactly: `１．５` vs `１，５`
vs `1.5`, `10：30` vs `10．30`, `1 : 5` vs `1 . 5`, `1/5` vs `1:5`, `1 – 5` vs
`1 - 5`, and `1 000` vs `1000` are DIFFERENT. A change in Unicode normalization only (NFC vs NFD,
e.g. Hangul from a macOS file name) is DIFFERENT. `review` and `doctor` name it
("same text in another Unicode normalization form (NFC -> NFD)") and suggest
`unicodedata.normalize("NFC", s)` before capturing.
In structured payloads only string values are compared loosely, and only when
they are prose.

| persona | rules prose SAME when... | use it for |
|---|---|---|
| `persona:editor` | only letter case, spaces within a line, or sentence punctuation (`. , ; : !` before a space or the end; `。、，．；：！` anywhere), never anything between two digits, differ. Every word must match; a unit after a number keeps its case (`5 mW` vs `5 MW`). | CI without a key: collapses cosmetic rewording only |
| `persona:lenient` | as editor, and ordinary words may also change (`went up` vs `rose`). Can pass `approved` vs `denied`. | tests and demos only, **never real gating** |
| `persona:strict` | never | forcing every mismatch to stay breached |

Any provider:model can plug in as a backend. Each ruling is recorded once per
fingerprint pair in `.nightward/judge/` (one JSON file per ruling) — a **committed
ledger**. Each entry records the behavior, model, verdict, reason, and the old and
new wording it ruled on (up to 1,000 chars each), so every ruling lands in the PR
diff. **Review ledger diffs like baseline diffs**: a ledger entry can turn a breach
green. When a run adds ruling files, `run` names them and says to commit
`.nightward/judge/` with the baseline (MCP: `warnings.judge_added`), so CI and
teammates gate on the same rulings.

- A model judge (`anthropic:*`) rules once per pair. Its ruling is replayed from
  the ledger after that, so the model's own nondeterminism can't wobble the gate and
  fresh clones and CI replay verdicts without a key. A replayed ruling is marked
  `(replayed from the committed ledger, not ruled this run)` in `review`, and
  `"judge_replayed": true` in `status --json` and MCP: whoever last edited the
  ledger made that ruling. An entry is replayed only when it describes exactly the
  pair being judged: its file is named by the hash of its key (the two fingerprints
  and the judge), and its `behavior`, `model`, `old` and `new` fields match the
  behavior and wording being judged. So the fields a reviewer reads are the ones that
  decide. Any other entry is not replayed (`warning: judge ledger entry <file> (...)
  - not replayed`): the pair is ruled again, or compared exactly when the judge is
  unavailable. A legacy `judge_verdicts.json` entry follows the same rule and is
  moved into `judge/` when it is used.
- A persona is deterministic and free, so it rules again on every run and its
  ledger entries are a record only. A hand-edited persona entry (`DIFFERENT` ->
  `SAME`) never changes the verdict: `run` warns that the ledger entry did not match
  the persona's ruling and rewrites it, which shows up in `git diff`.

Rulings are visible wherever the verdict is:

- `nightward run` and `nightward status` count and name the behaviors ruled SAME.
- `nightward review` lists every behavior the judge ruled SAME, with its diff,
  even when the boundary is intact. A wrong SAME is a hole in the gate, so audit them,
  and overrule one with `nightward reject <name>`: a human rejection beats the judge,
  so the behavior is unapproved again and the gate breaches until the code is fixed.
- `status --json` (and MCP) carry `judged`, `judge_model` and `judge_reason` on each
  change, and a `judged_same` list of `{name, group, judge_model, judge_reason}`.
- The dashboard has a "ruled semantically SAME" section with the diffs.

A judge that can't rule (SDK not installed, no
`ANTHROPIC_API_KEY`, API error, unparseable reply) falls back to the exact
comparison, so the gate fails closed, and says so: `nightward run` prints
`warning: judge <spec> unavailable (<reason>); N semantic behavior(s) compared
exactly`, and the report and `status --json` carry
`"judge": {"spec", "unavailable", "compared_exactly"}`.
