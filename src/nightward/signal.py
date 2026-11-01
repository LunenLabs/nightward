"""Machine-readable boundary status — the stop-condition oracle for agent loops.

A ralph-style loop reads `nightward status --json` and only emits its completion
promise when {"boundary": "intact"}. Any other value means "not done":
"breached" (unapproved changes), "partial" (nothing unapproved among what ran,
but approved behaviors were not checked: their test was deselected), "incomplete"
(nothing unapproved, but capture tests failed or errored - fix them), "stale"
(the baseline, capture or rejections moved since the last report - re-run), or "unknown" (no
report yet).
"""
from __future__ import annotations

# What an agent reads of each diff (with_diff): enough to tell an intended change
# from a regression, bounded so a large capture can't flood its context. Reading
# it marks nothing reviewed - only `review` and `view` record what a human saw.
AGENT_DIFF_LINES = 40
AGENT_DIFF_BUDGET = 10 * AGENT_DIFF_LINES   # lines across all changes of one payload


def _with_diffs(changes: list[dict], items: list[dict]) -> None:
    """Add each item's diff, cut to the per-change and per-payload budget;
    diff_cut counts the lines left out (diff is None once the budget is spent)."""
    budget = AGENT_DIFF_BUDGET
    for change, it in zip(changes, items, strict=True):
        lines = (it.get("diff") or "").splitlines()
        take = min(len(lines), AGENT_DIFF_LINES, budget)
        budget -= take
        change["diff"] = "\n".join(lines[:take]) if take or not lines else None
        change["diff_cut"] = len(lines) - take


def status_payload(report: dict | None, *, stale: bool = False,
                   with_diff: bool = False) -> dict:
    """stale=True means the baseline, capture or rejections changed since this report; its
    verdict no longer applies, so boundary reads "stale" instead of intact/breached.
    with_diff=True adds each change's diff, bounded (AGENT_DIFF_LINES) - for agents."""
    if report is None:
        return {"boundary": "unknown", "unapproved": 0, "changes": [], "judged_same": [],
                "not_run": [], "narrowed": False, "stale": False, "incomplete": None,
                "generated_at": None, "judge": None, "scrub_rules": [],
                "env_mismatch": None, "capture_coverage": None}

    # Every field of a change except its (possibly large) diff and its review
    # token, so judged rulings (judged / judge_model / judge_reason) and
    # standing rejections (rejected / rejected_by) reach agents and CI.
    items = [it for group in report.get("blast_radius", {}).values() for it in group]
    changes = [{k: v for k, v in it.items() if k not in ("diff", "token")} for it in items]
    if with_diff:
        _with_diffs(changes, items)
    boundary = report.get("boundary", "unknown")
    incomplete = report.get("incomplete")
    if stale:
        boundary = "stale"
    elif incomplete and boundary in ("intact", "partial"):
        boundary = "incomplete"   # a failing capture test is never "done"
    # judge_replayed only when true: the ruling came from the committed ledger,
    # the judge did not rule again this run (D22).
    judged_same = [
        {k: it.get(k) for k in ("name", "group", "judge_model", "judge_reason")}
        | ({"judge_replayed": True} if it.get("judge_replayed") else {})
        for it in report.get("judged_same", [])
    ]
    return {
        "boundary": boundary,
        "unapproved": report.get("unapproved", 0),
        "changes": changes,
        "judged_same": judged_same,
        # approved behaviors whose test was deselected (-k/-m): not checked (D21)
        "not_run": list(report.get("not_run") or []),
        "narrowed": bool(report.get("narrowed")),
        "stale": stale,
        "incomplete": incomplete,
        "generated_at": report.get("generated_at"),
        "judge": report.get("judge"),
        # custom scrub rules active in the last run: {rule (where it was
        # registered), values, behaviors, placeholder} (R5-POL-03)
        "scrub_rules": list(report.get("scrub_rules") or []),
        # changed behaviors whose baseline was approved on another OS/Python:
        # {"capture": env, "behaviors": {name: env}} or None - suspect the
        # environment before the code
        "env_mismatch": report.get("env_mismatch"),
        # [tool.nightward] coverage: {roots, functions, executed, uncovered,
        # unreadable} or None - what no capture test ran; never part of boundary
        "capture_coverage": report.get("capture_coverage"),
    }
