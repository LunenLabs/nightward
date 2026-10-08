"""Machine-readable boundary status — the stop-condition oracle for agent loops.

A ralph-style loop reads `nightward status --json` and only emits its completion
promise when {"boundary": "intact"}. Any other value means "not done":
"breached" (unapproved changes), "incomplete" (nothing unapproved, but capture
tests failed or errored - fix them), "stale" (the baseline or capture moved since
the last report - re-run), or "unknown" (no report yet).
"""
from __future__ import annotations


def status_payload(report: dict | None, *, stale: bool = False) -> dict:
    """stale=True means the baseline or capture changed since this report; its
    verdict no longer applies, so boundary reads "stale" instead of intact/breached."""
    if report is None:
        return {"boundary": "unknown", "unapproved": 0, "changes": [], "judged_same": [],
                "not_run": [], "narrowed": False, "stale": False, "incomplete": None,
                "generated_at": None, "judge": None}

    # Every field of a change except its (possibly large) diff and its review
    # token, so judged rulings (judged / judge_model / judge_reason) and
    # standing rejections (rejected / rejected_by) reach agents and CI.
    changes = [
        {k: v for k, v in it.items() if k not in ("diff", "token")}
        for items in report.get("blast_radius", {}).values()
        for it in items
    ]
    boundary = report.get("boundary", "unknown")
    incomplete = report.get("incomplete")
    if stale:
        boundary = "stale"
    elif incomplete and boundary == "intact":
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
    }
