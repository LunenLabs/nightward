# AI agents (`nightward mcp`)

[nightward](../README.md) reference. The full MCP surface: setup, tools, every returned field, and the rules for an agent loop.


An agent loop needs a definition of "done" that it can check but not change.
`nightward mcp` is a stdio [MCP](https://modelcontextprotocol.io) server that lets
the agent **run** the gate and **read** its verdict. It can't approve anything.

```bash
pip install "nightward[mcp]"                      # the mcp 1.x SDK (2.x is not supported yet)
claude mcp add nightward -- nightward mcp         # e.g. Claude Code; run it in the project root
```

Other hosts take the usual JSON entry. Start the server in the project root: it
resolves `path` against its own working directory and gates the store `.nightward`
there (or `nightward mcp --dir PATH`). Started in a subdirectory, both tools refuse
with "no nightward store at ..., but found ..." instead of creating a second, empty
store there. The store is the human's choice, like the judge: a tool call's `dir` may
only name that same store, and any other is refused. Otherwise an agent could copy
the store into an ignored folder, forge its baseline, and get "intact" with nothing
in `git status`.

```json
{"mcpServers": {"nightward": {"command": "nightward", "args": ["mcp"], "cwd": "/path/to/project"}}}
```

| tool | arguments | what it does |
|---|---|---|
| `nightward_run` | `path="."` (what pytest runs), `timeout=600` (seconds; on expiry the capture is left untouched and the last report invalidated); `dir` is optional and may only name the server's store | runs the tests, captures behaviors, recomputes the boundary |
| `nightward_status` | `dir` (optional, as above) | reads the last run's verdict without running anything |

Both return the `status --json` shape: `boundary`, `unapproved`, `changes` (`name`,
`kind`, `group`, plus `judged`, `judge_model`, `judge_reason` when a judge ruled, and
`judge_replayed` when that ruling came from the ledger; `rejected`, `rejected_by` when
the payload is a standing rejection), `not_run` (`{name, group}` of approved behaviors
whose test was deselected) and `narrowed`, `judged_same`, `stale`, `incomplete`
(`{"failed": n, "errors": m}` or null), `generated_at`, `judge`, `scrub_rules` (the
custom scrub rules of the last run and what they replaced), `env_mismatch`
(`{"capture": {os, python}, "behaviors": {name: {os, python}}}` for changed
behaviors approved on another OS or Python, else null - a hint for the human,
never a verdict), and `store` (the
absolute path of the store the verdict comes from). `nightward_run` also returns
`path` (what pytest ran), so a loop driver can check both. `boundary` is one of:

| `boundary` | meaning | what the agent should do |
|---|---|---|
| `intact` | no unapproved change | done |
| `breached` | unapproved changes | fix the code, or stop and ask a human to approve |
| `partial` | nothing unapproved among what ran, but approved behaviors were not checked (deselected, see `not_run`) | run them: remove the `-k`/`-m` selection (it can't be waived over MCP) |
| `incomplete` | nothing unapproved, but capture tests failed or errored | fix the failing tests (see `incomplete` and `pytest_output_tail`) |
| `stale` | the baseline, capture or rejections moved since the report | call `nightward_run` again |
| `unknown` | no report yet | call `nightward_run` |

`nightward_run` adds `warnings`: `skipped`, `failed`, `errors`, `deselected`,
`xfailed`, `scrubbed` (values the default scrubbers masked), `scrub_unmatched`
(custom scrub rules that matched nothing), `scrub_rules` (per custom rule: values and
behaviors it replaced this run, whether that changed, and whether its replacement is a
`<PLACEHOLDER>`), `pytest_returncode`, and
`pytest_output_tail` (pytest's last lines, so the agent can see why tests failed).
The agent is done when `boundary` is `"intact"` and `stale` is false.

Rules for the loop:

- **Call `nightward_run` after every code edit.** `nightward_status` and `gate` only
  report the last run. They don't notice code edited since then, and `stale` only
  covers a baseline that changed after the run.
- **The agent can't approve.** `approve` and `reject` are not exposed. If the agent
  that makes a change could also approve it, the gate would turn into a changelog.
  A human approves with the CLI and commits the baseline.
- **The judge is the humans' committed choice.** Behaviors approved as `semantic=True`
  are judged by `nightward mcp --judge <provider:model>` if the server was started
  with it, else by the committed `[tool.nightward] judge`. MCP ignores
  `$NIGHTWARD_JUDGE` and any `nightward run --judge` override, so a one-off demo run
  can't change the agent's gate. The tool has no judge argument, so the agent can't
  pick a lenient judge, and it gets the same verdict as a plain `nightward run`.
