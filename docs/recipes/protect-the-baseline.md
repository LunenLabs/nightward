# Recipe: make "only a human approves" hold on GitHub

[nightward](../../README.md) recipe. How to turn the human-only approval rule from
a convention into something your repository enforces.

nightward keeps `approve` away from the agent's tools (MCP exposes only run and
status), but it is not a sandbox: an agent with a shell can run
`nightward approve`, or copy a capture into `baseline/` by hand
([threat model](../threat-model.md)). Every way to open the gate ends in a **diff to
files in the store**, so the place to enforce the rule is code review of those
files. On GitHub that takes three settings.

## 1. Make humans the owners of the decision files

Add a `CODEOWNERS` file (`.github/CODEOWNERS`) that names the people or team who
may approve behavior changes:

```text
# nightward: approving, rejecting and removing behaviors are human decisions
/.nightward/baseline/   @your-org/behavior-owners
/.nightward/rejected/   @your-org/behavior-owners
/.nightward/judge/      @your-org/behavior-owners

# what decides "the same": the committed judge and the scrub rules
/pyproject.toml         @your-org/behavior-owners
conftest.py             @your-org/behavior-owners
```

Every way to change the verdict lands in one of these paths:

| what someone does | the diff it leaves |
|---|---|
| approves a NEW or CHANGED behavior | `baseline/<name>.approved.json` added or changed |
| removes a behavior (or deletes its test) | the test stays REMOVED until `baseline/<name>.approved.json` is deleted |
| rejects a change, or drops a rejection | `rejected/` |
| loosens the judge or forges a ruling | `pyproject.toml`, `judge/` |
| masks a value with a scrub rule | a `conftest.py` |

A plain `conftest.py` pattern matches the file at any depth. Scrub rules can also be
registered in a test module; if your team does that, own the test directory too.

## 2. Require the owners' review on the protected branch

In **Settings → Rules → Rulesets** (or **Branches → Branch protection rules**) for
`main`:

- **Require a pull request before merging**, with
  - **Require review from Code Owners**,
  - **Dismiss stale pull request approvals when new commits are pushed** (an
    approval must cover the baseline the PR finally merges).
- **Require status checks to pass**: the job that runs the
  [nightward action](ci-github-actions.md) (or `nightward run` + `nightward gate`).
- **Do not allow bypassing the above settings**, and keep the bypass list empty
  or limited to humans.

## 3. Give the agent an identity that is not an owner

Let the agent push and open PRs with its own account or app token, not a
code owner's. GitHub never counts the author's approval of their own PR, and an
agent that isn't in `behavior-owners` can't approve someone else's baseline diff
either. Don't give that token admin rights or a place on the bypass list.

## What this does and doesn't guarantee

With the three settings, nothing reaches `main` with a changed baseline, judge,
rejection or scrub rule unless a behavior owner reviewed that diff, and the
required check re-captures from source, so a forged `report.json` can't pass.

It doesn't cover what nightward never sees: behaviors no test captures, and changes
outside the protected branch. A local `nightward gate` (or an agent's
`nightward_status`) is still only as trustworthy as the machine it runs on; the
merge is where the rule holds.
