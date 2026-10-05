# Recipe: gate every PR with nightward on GitHub Actions

Two jobs: **gate** blocks the PR when the behavior boundary is breached, and
**blast-radius** uploads the read-only dashboard as an artifact so reviewers see
*what moved* without checking out the branch.

Assumptions: your repo commits `.nightward/baseline/` (run `nightward init` once —
it writes the right `.gitignore` rules), and your tests capture behaviors via the
`behavior` fixture.

```yaml
name: nightward

on:
  pull_request:

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install nightward  # plus your project deps
      - name: Capture behaviors and gate against the approved baseline
        run: |
          nightward run .           # exit 1 if any capture test failed or errored
          nightward gate            # exit 1 = breached, incomplete or stale = PR blocked

  blast-radius:
    if: always()                    # build the explanation even when the gate fails
    needs: gate
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install nightward
      - run: nightward run . || true
      - run: nightward view --no-serve --out blast-radius
      - uses: actions/upload-artifact@v4
        with:
          name: blast-radius
          path: blast-radius
```

## The review loop this creates

1. An agent (or a human) opens a PR. CI runs `nightward run` + `gate`.
2. **Intact** → merge as usual; behavior provably didn't move.
3. **Breached** → the check fails. The reviewer opens the `blast-radius`
   artifact, sees exactly which behaviors moved and their diffs, and decides:
   - intended → run `nightward approve <name>` locally and push; the approval
     itself appears in the PR diff as a `*.approved.json` change, reviewable
     like any code;
   - regression → fix the code (optionally `nightward reject <name>` for the
     audit trail) and push again.

## Semantic judge in CI (nondeterministic AI output)

If you capture LLM output with `semantic=True`, give the run a judge. Use a real
model on CI with a secret, and note the verdict cache keeps token spend at one
call per new fingerprint pair:

```yaml
      - name: Capture and gate (with semantic judge)
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          pip install "nightward[judge]"
          nightward run . --judge anthropic:claude-haiku-4-5
          nightward gate
```

No key available (forks, dry runs)? `--judge persona:editor` is a deterministic,
key-free stand-in: it collapses pure case/punctuation/whitespace rewording and
keeps everything else breached. Judge failures always fall back to the exact
comparison — the gate fails closed.

## Rules worth keeping

- `gate` blocks, `view` explains. Never publish a real store's dashboard to a
  public site — captured output may contain sensitive data; CI artifacts stay
  scoped to the repo's access.
- Approval is a human commit, never a CI step. Auto-approving in CI turns the
  gate into a changelog (see the MCP isolation model in CLAUDE.md).
