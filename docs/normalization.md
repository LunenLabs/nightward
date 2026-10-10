# What nightward normalizes (what counts as "the same payload")

[nightward](../README.md) reference. Default scrubbing, custom scrub rules, behavior names, non-JSON values, diffs, and what `nightward doctor` suggests.


Two captures are equal when their **normalized JSON** is equal. Know what the
normalization erases, because a change there never trips the gate:

- **Timestamps and UUIDs (default scrubbers).** Every ISO-8601 datetime with a `T`
  (`2024-01-05T09:00:00+00:00`) becomes `"<TIMESTAMP>"` and every UUID becomes
  `"<UUID>"`, anywhere in the payload (values *and* keys). This stops run-to-run
  noise, and the placeholders are visible in `baseline/*.approved.json`.
  `nightward run` prints how many values were masked. If datetimes or UUIDs are
  your **output** (deadlines, event times, `uuid5` ids), opt out:

  ```python
  behavior("sla_deadlines", deadlines, scrub=False)   # this behavior: no scrubbing at all

  # the root conftest.py - every behavior: built-in timestamp/uuid scrubbers off,
  # your own register/register_field rules still apply
  from nightward import scrub
  scrub.disable_defaults()
  ```

- **Dict key order.** Keys are sorted, so `{"b": 1, "a": 2}` equals `{"a": 2, "b": 1}`.
  If order is part of the contract (CSV column order, `df.to_dict("records")`
  feeding a positional loader), capture it explicitly: `list(df.columns)` or
  `df.to_csv(index=False).splitlines()`.
- **Key types and containers (JSON semantics).** Non-string keys become strings
  (`{3: 2}` equals `{"3": 2}`) and tuples become lists. Capture `type(k).__name__`
  or a list of pairs if the type matters.
- **Multi-line strings** (HTML bodies, CLI stdout, rendered manifests) are one JSON
  value. `review` and the dashboard diff them line by line, but the committed
  baseline file stores one escaped string, so its git diff is one long line -
  capture `text.splitlines()` when you want per-line git diffs too.

**Names are file names.** A behavior name becomes `<store>/baseline/<name>.approved.json`:
no whitespace or path characters, at most 200 characters, and on Windows the whole store
file path must stay under 260 characters (Git for Windows can't add longer paths without
`core.longpaths`). A name that would cross that limit fails its test at capture time -
keep names short when the project lives in a deep directory.

**Non-JSON values are rejected, never coerced.** A capture fails its test with the
path, the type and a fix, e.g. `behavior 'daily': payload is not JSON-serializable: value at
$.units is numpy.int64, which is not JSON - use .item() (or int()/float()/bool())`.
Common conversions:

| value | capture |
|---|---|
| numpy scalar (`np.int64`, `np.float32`, `np.bool_`) | `x.item()` |
| `np.ndarray`, `pd.Series` | `x.tolist()` |
| `pd.DataFrame` | `df.to_dict("records")` (or `df.to_csv(index=False).splitlines()`) |
| `date` | `x.isoformat()` |
| `datetime`, `pd.Timestamp` | `x.isoformat()`, but an ISO date-time is masked as `"<TIMESTAMP>"` by the default scrubber: if it is your output (a due date, an event time), also pass `scrub=False` (or call `scrub.disable_defaults()`) |
| `Decimal` | `str(x)` (keeps the exact digits) |
| `set` | `sorted(x)` |
| `bytes` | `x.decode()` or `x.hex()` |
| `NaN` / `inf` (e.g. the mean of an empty group) | `None` or a marker string such as `"NaN"` |
| dict with mixed key types (`{1: "a", "b": 2}`) | `str` keys |

`np.float64` is accepted as is: it subclasses Python's `float`. `np.float32` and the
numpy integer types don't, so convert them.

Diffs are for reading, never for the verdict: the verdict comes from
fingerprints, and a diff is capped at 2,000 lines (huge payloads end with a
"diff truncated" marker; very large scattered changes are compared by line
position), so `run`/`approve` stay fast on big captures.

Some changes print the same on both sides: NO-BREAK SPACE vs NARROW NO-BREAK SPACE
after a CLDR upgrade, a zero-width space or bidi mark, doubled or trailing
whitespace, a Cyrillic `а` in place of a Latin `a`, or `−` (minus) in place of `-`.
For such a -/+ pair, `review` and the dashboard escape only the characters that
differ (`"1 234"` -> `"1 234"`) and add a `? invisible or look-alike
change: U+00A0 NO-BREAK SPACE -> U+202F NARROW NO-BREAK SPACE` line, and `doctor`
names them in its note. This is display only and never affects the fingerprint.
The committed baseline file stores the raw characters, so its git diff still
looks unchanged; use `nightward review` to read it.

Add your own rules for project-specific noise (prefer `register_field` - it
replaces a JSON value and can't corrupt the payload):

```python
from nightward import scrub
scrub.register_field("request_id")                 # mask this key at any depth
scrub.register(r'"ord_\d+"', '"<ORDER_ID>"')       # regex over the JSON text
```

**Rules are test-owned, and their scope follows the file that registers them.** Call
`scrub.register`, `register_field` and `disable_defaults` directly in a `conftest.py`
or a test module (pytest's `python_files`). The rule applies only to behaviors
captured by tests under that file's directory, like a conftest's fixtures. Rules in
the root `conftest.py` cover the whole suite; `scrub.register_field("token")` in
`services/orders/conftest.py` or `services/orders/test_orders.py` masks orders' tokens
but never a `token` field in `services/billing`. The scope doesn't depend on which
directories a run collected, so `nightward run services/billing` captures exactly what
the whole-suite run does. A call from any other file (product code, a shared helper,
a plugin) is refused with an error naming the file and line: a rule there could
rewrite a regressed value back into the approved one with nothing in the test diff.
`nightward doctor` names the conftest.py each suggested rule belongs in.

**What custom rules replaced is reported.** `nightward run` prints, per custom rule,
how many values it replaced and in which behaviors whenever that count changes since
the last run (`note: scrub rule register(r'...') in conftest.py replaced 1 value(s)
this run (was 0) in route`). A replacement that is not a `<PLACEHOLDER>` is called
out as a rewrite, not a mask. MCP `warnings.scrub_rules` carries the same data
(`rule`, `values`, `behaviors`, `was`, `changed`, `placeholder`). The active rules stay
visible after that run: `review`, `status` (and `status --json` / MCP `scrub_rules`:
`rule` with where it was registered, `values`, `behaviors`, `placeholder`) and the
dashboard list every custom rule and what it replaced in the last run.

`register()` patterns run over the payload's **canonical JSON text**, not over the
decoded strings: pretty-printed (`"key": "value"`, keys sorted), and inside a string
value every `"` is written `\"` and every newline `\n`. A pattern copied from what
the app emits (`name="csrf_token" value="..."`, `"request_id":"req_..."`) therefore
never matches, and `^`/`$` never see the lines of a multi-line body. Match the
escaped form instead:

```python
# <input type="hidden" name="csrf_token" value="3f9a..."> inside an HTML body
scrub.register(r'csrf_token\\" value=\\"[0-9a-f]{32}', r'csrf_token\\" value=\\"<CSRF>')
```

When the volatile part sits inside a string, masking it in the test before
capturing (`re.sub(...)` on the body) is often simpler. `nightward run` warns about
every `register`/`register_field` rule that matched nothing in that run
(`warning: scrub rule register(r'...') matched nothing in this run`), so a rule that
silently does nothing can't pass for handled noise.

`nightward doctor` sees one before/after pair, which is no evidence that a value is
noise. It calls a value volatile only when the value itself shows it. A date or an
order can be the product (a deadline, an event sequence), so those look like a real
change first:

| doctor sees | it says | it suggests |
|---|---|---|
| a random token behind a stable prefix (`chatcmpl-…`, `call_…`, `req_…`, a CSRF value in HTML) | `~` volatile | a `scrub.register(...)` pattern anchored on that prefix, with a base62 class and open length (`[0-9A-Za-z]{8,}`), never the whole field or body |
| a date-time, HTTP date or Unix timestamp (also as a string, e.g. `X-RateLimit-Reset`) | `*` looks like a real change | only *if it is not part of the contract*: `scrub.register_field(key)` for that key, or a pattern anchored on the text before it; for a date in a list, "mask it at capture time" (no global date pattern) |
| several dates that all moved by the same amount | `*` looks like a real change ("all 2 values moved by -1 day") | nothing |
| a list with the same elements in a new order | `*` looks like a real change | only *if it is not part of the contract* (e.g. a set): sort it before capturing |
| the same rows in a new order with float noise in some values (an unordered `GROUP BY` on a parallel engine) | `*` looks like a real change, naming the columns with noise (`$[*][2]`) and the largest drift (`up to 15 ULP`) | only *if order is not part of the contract*: sort (`ORDER BY` a key) and round to the number of digits it names, checked to make both samples equal |
| the columns of one frame (`df.to_dict("list")`: sibling lists of one length) in a new order, every row intact | `*` looks like a real change, one finding for the frame | only *if row order is not part of the contract*: sort the **rows** by a key (`df.sort_values(...)`) or capture records; never sort the columns independently |
| one column's values permuted while the others stayed (values moved between rows) | `*` looks like a real change ("values moved between rows (2 of 12; cust unchanged)") | nothing: it is a real change |
| a list that grew or shrank | `!` names each element added or removed with its index (`1 element added at [2]: {...}`) and whether the rest kept its order | nothing: review it |
| a float within a few ULPs (float32 values such as embeddings), or a float64 that drifted by up to 1e-11 relative and under 1e-6 absolute (a sum in another order) | `~` float noise | round before capturing, to the most significant digits (at most 12, or 6 for float32) that make every drifted value in that path equal, e.g. `float(f"{x:.4g}")`; no mask. Rounding lowers the odds of a flip but can't rule it out: a value near a rounding boundary can still flip on another machine. For vectors, capture what the product uses (top-k ids, a ranking). Integral floats and deltas of 1 or more are never noise |
| rounded floats (5+ significant digits, 4+ decimals) that differ by 1 in the last digit | `*` looks like a real change | *if the capture rounds float noise*, these are rounding-boundary flips: fewer digits (checked on these values), or capture what the product uses |
| a string holding JSON (a tool call's `arguments`) whose parsed value is unchanged | `~` formatting only | capture `json.loads(...)` of it; when the parsed value did change, doctor names the inner path (`arguments<json>.invoice_id`) |
| the same text in another Unicode normalization form (NFC -> NFD) | `*` looks like a real change, named as such | only *if the form is not part of the contract*: `unicodedata.normalize("NFC", s)` before capturing |
| `0.0` -> `-0.0` | `~` sign of zero | `x + 0.0` before capturing |
| a changed content hash, a type change, a new key, anything else | `*` / `!` looks like a real change | nothing: review, then approve or fix |

Every suggested rule is checked to make both samples equal. It is withheld when it
would also match a stable value anywhere else in the capture, and for behaviors
captured with `scrub=False`. If a value marked as a real change changes again on a
re-run with no code edits, it is volatile: apply the conditional suggestion (sort,
round, normalize), or mask
it at capture time in that test.
