# nightward

**A regression gate for AI-made changes. Your AI agent can run the gate, but it can't approve it.**

![15-second demo: an AI fixes one line, every test passes, nightward shows three customer-facing behaviors moved, the gate stops CI, and only a human can approve](https://raw.githubusercontent.com/LunenLabs/nightward/main/docs/assets/demo.gif)

When an AI agent fixes A, it often moves B, C and D too, and the test suite stays
green because no test pins down B, C or D. You find the side effects later, fix
them, and the fixes move something else. An agent loop has no reliable way to know
when it is done.

nightward gives that loop a definition of "done" that the agent can check but not
change:

1. Your tests capture what the code does today with a `behavior(...)` pytest
   fixture. You don't write expected values.
2. A human reviews those captures and approves them once. That baseline is a set
   of JSON files committed next to your code.
3. After every change, nightward captures again and lists everything that moved,
   grouped by feature (the *blast radius*). The gate stays closed until a human
   approves each change as intended or the code is fixed.

The agent gets `run` and `status`, on the command line or as MCP tools. Approving
is a human CLI command, and its result is a file diff that goes through code review.

```text
nightward run       # capture behaviors and compare them with the approved baseline
nightward review    # a human reads the diffs
nightward approve   # the human accepts what review showed (intended changes only)
nightward gate      # exit 0 only when nothing unapproved remains (CI, agent loops)
```

nightward is **not** a test generator, and it has no idea what is *correct*. It
only makes sure that no change to a captured behavior passes silently.

| Compared with | How nightward differs |
|---|---|
| NL→test generators | nightward **gates** changes, it doesn't author tests |
| plain snapshot libraries | nightward aggregates a **blast radius**, emits a machine **stop-signal** for agent loops, and keeps approval human-only |
| ordinary regression tests | no hand-written expected values, and a semantic judge can gate nondeterministic AI text |

What's new in 0.3, and what changes for 0.2 users: see the [CHANGELOG](https://github.com/LunenLabs/nightward/blob/main/CHANGELOG.md).

## Install

nightward needs Python 3.10+. Installing it also registers its pytest plugin, so
the `behavior` fixture is available in every test.

```bash
pip install nightward
pip install "nightward[mcp]"      # + MCP server for AI agents
```

Other extras: `[judge]` (the Anthropic SDK, for the LLM judge) and `[docs]` (the
PDF, Word and Excel adapters).

## Quickstart (5 minutes)

In an empty directory, create a tiny app and a test that captures its behavior.
`app.py`:

```python
def checkout_total(items):
    subtotal = sum(i["price"] * i["qty"] for i in items)
    tax = round(subtotal * 0.1, 2)
    return {"subtotal": subtotal, "tax": tax, "total": round(subtotal + tax, 2)}


def user_login(username):
    return {"user": username, "status": "ok", "roles": ["member"]}
```

`test_app.py` writes no expected values. It captures what the code returns
(for anything bigger than this, shape the payload so a human can review it:
[payload design](https://github.com/LunenLabs/nightward/blob/main/docs/payload-design.md)):

```python
from app import checkout_total, user_login

CART = [{"price": 10.0, "qty": 2}, {"price": 5.5, "qty": 1}]


def test_checkout(behavior):
    behavior("checkout_total", checkout_total(CART), group="billing")


def test_login(behavior):
    behavior("user_login", user_login("alice"), group="auth")
```

**1. Approve the current behavior as the baseline.**

```bash
git init                    # skip in an existing repo
nightward init              # creates .nightward/ and git-ignores its per-run files
nightward run .             # runs pytest and captures; lists what moved (names only)
nightward review            # shows each diff; approve covers only what review showed
nightward approve --all
git add .gitignore .nightward   # commit baseline/, rejected/, judge/ (init ignores per-run files)
git add app.py test_app.py && git commit -m "nightward baseline"
```

`run` finds two NEW behaviors, `review` prints their JSON, and `approve` makes them
the baseline:

```text
Boundary: breached (2 unapproved)
unchanged=0 changed=0 new=2 removed=0
...
approved checkout_total (c9ff6ed9)
approved user_login (b86be5a5)

Boundary: intact
unchanged=2 changed=0 new=0 removed=0
```

**2. Make the kind of change an agent makes.** In `app.py`, change the tax rate
`0.1` to `0.2` (in bash: `sed -i 's/subtotal \* 0.1/subtotal * 0.2/' app.py`).
Then:

```bash
nightward run .             # the tests still pass
nightward gate              # exit code 1: not done
nightward status --json     # the same verdict, for a script or an agent
nightward approve --all     # refused: nobody has reviewed this change yet
nightward review            # the diff a human decides on
```

```text
Boundary: breached (1 unapproved)
unchanged=1 changed=1 new=0 removed=0

group: billing
  - [CHANGED] checkout_total

boundary breached (1 unapproved) (as of the last run, ...)

{"boundary": "breached", "unapproved": 1, "changes": [{"name": "checkout_total", "kind": "CHANGED", "group": "billing"}], ...}

error: checkout_total changed since your review (the capture now in the store was taken by `nightward run` at ...). Run `nightward review checkout_total`, then approve what it shows.

[CHANGED] checkout_total
--- approved
+++ received
@@ -1,5 +1,5 @@
 {
   "subtotal": 25.5,
-  "tax": 2.55,
-  "total": 28.05
+  "tax": 5.1,
+  "total": 30.6
 }
```

**3. Decide.** If the change is a regression, fix the code: put `0.1` back, run
`nightward run .` again, and it reports `Boundary: intact`, so `nightward gate`
exits 0. If the change is intended, run `nightward approve --all` (it now
succeeds, because `review` showed you this diff) and commit the updated
`.nightward/baseline/checkout_total.approved.json`.

**4. Optional: see the blast radius in a browser.** `nightward view` builds a
static, read-only dashboard and serves it on localhost. `nightward view
--no-serve` only builds `nightward-site/`, which `init` already git-ignored.

For a bigger cascade (one tax tweak moves three customer-facing behaviors), run
the [petshop example](https://github.com/LunenLabs/nightward/tree/main/examples/petshop) from a clone of this repo.

## What to commit

| path | commit? | what it is |
|---|---|---|
| `.nightward/baseline/` | yes | the approved behaviors: the boundary |
| `.nightward/rejected/` | yes | changes a human confirmed as regressions |
| `.nightward/judge/` | yes | the semantic judge's rulings (review them like the baseline) |
| `pending/`, `report.json`, `run_meta.json`, `reviewed.json`, `.lock` | no | per-run state, ignored by `nightward init` |
| `nightward-site/` | no | the dashboard build: it contains your captured data |

## In CI: the GitHub Action

Set up Python and your project's dependencies, then gate the PR with one step.
It runs your tests, writes the verdict and the diffs that moved to the job
summary, and fails the job unless the boundary is intact. It never approves:
intended changes are approved locally (`nightward review`, `nightward approve`)
and arrive in the PR as a baseline diff.

```yaml
- uses: actions/checkout@v4
- uses: actions/setup-python@v5
  with:
    python-version: "3.12"
- run: pip install -e .          # your project and its test dependencies
- uses: LunenLabs/nightward@v0.3.2
  with:
    path: tests                  # what pytest runs; default "."
```

Inputs: `path`, `dir` (default `.nightward`), `pytest-args` (one per line),
`allow-not-run`, `version`, `install`, `summary`; output: `boundary`. A full
workflow with the dashboard artifact is in the
[CI recipe](https://github.com/LunenLabs/nightward/blob/main/docs/recipes/ci-github-actions.md).

## Commands

| command | what it does |
|---|---|
| `nightward init` | create the store and the `.gitignore` rules (once per repo, and again after upgrading) |
| `nightward run [PATH] [-- PYTEST ARGS]` | run pytest, capture, compute the blast radius. Exit 1 if a capture test failed (the run is incomplete) |
| `nightward review [NAME...] [--group G] [--summary]` | show the diffs, or with `--summary` each changed path once with its old -> new values counted. Only `review` and `view` mark changes as reviewed |
| `nightward approve --all` / `NAME...` / `--group G` | promote reviewed NEW/CHANGED behaviors into the baseline. Bulk approval never removes anything |
| `nightward approve --remove NAME...` / `--remove-group G` | drop REMOVED behaviors: removal is always this explicit act |
| `nightward reject NAME` | confirm a reviewed change as a regression. The gate stays breached |
| `nightward gate [--allow-not-run] [--run]` | the verdict as an exit code: 0 intact; 1 breached, partial, stale or incomplete; 2 no store, no report, or an error. `--run` captures first, so the verdict covers the code as it is now |
| `nightward status [--json]` | the verdict with the change list. `--json` is the machine signal |
| `nightward doctor [NAME...]` | explain what moved, and suggest scrub rules only for values that are volatile by evidence |
| `nightward report` | the verdict from a capture that `pytest --nightward-record` already wrote in your CI job |
| `nightward view` | build and serve the read-only dashboard |
| `nightward mcp` | stdio MCP server for AI agents: run and status, never approve |
| `nightward --version` | the installed version |

`gate` and `status` report the **last run**, so run `nightward run` again after
every code edit (or use `nightward gate --run`). The full reference (removals, stale reports, merge conflicts,
locking, and using the plugin inside an existing pytest job) is in
[docs/commands.md](https://github.com/LunenLabs/nightward/blob/main/docs/commands.md).

## AI agents (`nightward mcp`)

An agent loop needs a definition of "done" that it can check but not change.
`nightward mcp` is a stdio [MCP](https://modelcontextprotocol.io) server with two
tools: `nightward_run` (run the tests, capture, recompute the verdict) and
`nightward_status` (read the last verdict). `approve` and `reject` are not
exposed: if the agent that makes a change could also approve it, the gate would
turn into a changelog. The judge and the store are the humans' committed choices,
so the agent can't pick a lenient judge or point the server at another store.

In Claude Code, install the plugin. It adds the MCP server and a skill that
tells Claude to run the gate after its edits and to hand intended changes to
you instead of approving them. nightward itself must be installed in the
environment you start `claude` from (your project's virtualenv):

```bash
pip install "nightward[mcp]"                      # in the project's environment
```

```text
/plugin install nightward --marketplace LunenLabs/nightward
```

`/nightward:status` then shows the verdict and what you need to review. Any other
MCP host can run the server directly:

```bash
claude mcp add nightward -- nightward mcp         # or your host's equivalent; run it in the project root
```

Both tools return the `status --json` shape. The agent is done only when
`boundary` is `intact` and `stale` is false:

| `boundary` | meaning | what the agent should do |
|---|---|---|
| `intact` | no unapproved change | done |
| `breached` | unapproved changes | fix the code, or stop and ask a human to approve |
| `partial` | nothing unapproved among what ran, but approved behaviors were not checked (deselected, see `not_run`) | run them: remove the `-k`/`-m` selection (it can't be waived over MCP) |
| `incomplete` | nothing unapproved, but capture tests failed or errored | fix the failing tests (see `incomplete` and `pytest_output_tail`) |
| `stale` | the baseline, capture or rejections moved since the report | call `nightward_run` again |
| `unknown` | no report yet | call `nightward_run` |

Setup for other MCP hosts, every argument and returned field, and the rules for
the loop are in [docs/mcp.md](https://github.com/LunenLabs/nightward/blob/main/docs/mcp.md).

## Gating AI text: the semantic judge

Free-text AI output changes its wording on every run, so an exact comparison
breaches every time. Mark such a behavior `semantic=True` and commit a judge in
`pyproject.toml`: `[tool.nightward] judge = "anthropic:claude-haiku-4-5"`, or the
key-free `persona:editor`. The judge only decides whether two outputs mean the
same thing; approval stays human. Every ruling is recorded in the committed
ledger `.nightward/judge/`, and a judge that can't rule falls back to the exact
comparison. Details are in
[docs/semantic-judge.md](https://github.com/LunenLabs/nightward/blob/main/docs/semantic-judge.md).

## What "intact" means

"Boundary intact" means no **captured** behavior changed, nothing more:

- The gate sees only what `behavior()` captures, so capture the behaviors you
  care about.
- The gate is a convention, not a sandbox. An agent with a shell could run
  `approve` or edit the baseline, but every bypass leaves a trace in git. Review
  baseline, ledger, scrub-rule and test diffs in every PR, protect your main
  branch, and let CI run `nightward run && nightward gate` from source.
- A semantic judge can be wrong, so its rulings are recorded for review.
- Normalization defines "the same": timestamps and UUIDs are masked by default
  (opt out with `scrub=False`), and dict key order is ignored.

Read the [threat model](https://github.com/LunenLabs/nightward/blob/main/docs/threat-model.md)
and [what nightward normalizes](https://github.com/LunenLabs/nightward/blob/main/docs/normalization.md)
before you rely on a green gate.

## Documentation

- [CHANGELOG](https://github.com/LunenLabs/nightward/blob/main/CHANGELOG.md): what's new in 0.3.0, and **upgrading from 0.2**
- [Commands and workflow](https://github.com/LunenLabs/nightward/blob/main/docs/commands.md):
  every command, removals, stale reports, merge conflicts, and the plugin in an
  existing pytest job
- [Designing payloads a human can review](https://github.com/LunenLabs/nightward/blob/main/docs/payload-design.md):
  aggregate by cause, record each input once, keep the machine out - read it before
  your first capture
- [What nightward normalizes](https://github.com/LunenLabs/nightward/blob/main/docs/normalization.md):
  scrubbing, custom scrub rules, names, non-JSON values, and `doctor`
- [Semantic judge](https://github.com/LunenLabs/nightward/blob/main/docs/semantic-judge.md)
- [AI agents (MCP)](https://github.com/LunenLabs/nightward/blob/main/docs/mcp.md)
- [Dashboard](https://github.com/LunenLabs/nightward/blob/main/docs/dashboard.md)
- [Document & artifact inputs](https://github.com/LunenLabs/nightward/blob/main/docs/adapters.md):
  PDF, Word, Excel, text and raw bytes
- [Threat model](https://github.com/LunenLabs/nightward/blob/main/docs/threat-model.md)
- [CI recipe: GitHub Actions](https://github.com/LunenLabs/nightward/blob/main/docs/recipes/ci-github-actions.md)

## Scope

In scope: pytest capture, the blast-radius diff, the gate, the loop signal,
scrubbing and `doctor`, the static dashboard, the MCP agent gate (run and status,
never approve), and the semantic judge. Out for now: PR-comment summaries,
call-graph grouping, and other languages. See [CONTRIBUTING.md](https://github.com/LunenLabs/nightward/blob/main/CONTRIBUTING.md).

MIT licensed.
