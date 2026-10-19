# Review depth

[nightward](../README.md) reference. How much a human reads for each group, and when
the agent hands a change to them.

If intended changes breach the gate often and every one gets the same full review,
people stop reading and run `approve --all`. An approval nobody read is an
automatic one. A review depth lets the people who own the project decide, per
group, how much each review costs:

```toml
[tool.nightward.review]
default = "standard"

[tool.nightward.review.groups]
"billing" = "exhaustive"
"search" = "detailed"
"debug-metrics" = "brief"
```

**No depth approves anything.** At every depth the boundary stays `breached`, and
`nightward gate` exits 1, until a human runs `approve`. A depth only decides how much
`review` shows, what a bulk `approve` covers, and when the agent asks.

## The depths

| depth | `review` shows | `approve` | the agent asks the human (`ask_human`) |
|---|---|---|---|
| `exhaustive` | the whole diff | by name only: `--all` and `--group` leave these behaviors and name them | `now` |
| `detailed` | the whole diff | names, `--group`, `--all` | `now` |
| `standard` (default) | the diff, up to `--max-lines` (60) | names, `--group`, `--all` | `now` |
| `light` | the summary (`--summary`): each changed path once, with old -> new counts | names, `--group`, `--all` | `end_of_task` |
| `brief` | one line per group: how many behaviors changed and the biggest move | names, `--group`, `--all` | `at_pr` |

`ask_human` is what the agent reads in `status --json` and from MCP. `now`: stop and
ask. `end_of_task`: keep working and list the changes in the hand-off. `at_pr`: keep
working; the CI gate blocks the merge until someone approves. An *unintended* change
is fixed whatever its depth: depth only moves when an *intended* change is handed
over.

## What no depth lowers

- **NEW and REMOVED behaviors get at least `standard`.** A first capture is where
  a wrong value gets approved as the truth, and a removal shrinks what the gate
  watches.
- **A standing rejection** (`.nightward/rejected/`) is shown at least at `detailed`.
- **`approve` still accepts only what `review` (or the dashboard) showed.** What a
  depth displays counts as shown: the people who set the depth decided that a
  `brief` group's one line is enough to approve on.

## Who sets it

The setting is read like the judge: from the `pyproject.toml` nearest the store,
never from the test path. A change to it is a reviewed diff like a baseline change.
Put `pyproject.toml` under CODEOWNERS
([protect the baseline](recipes/protect-the-baseline.md)), so an agent can't make
its own changes cheaper to approve. MCP doesn't expose the setting.

`nightward review --depth D` raises the depth for one review (for example
`exhaustive` before a release). It never lowers it. `approve` and `review` read the
setting as it is now, so a depth raised after a run applies at once. `status`,
MCP and the dashboard show the depth recorded at the last run.

The dashboard marks each card whose depth isn't `standard`.
