"""Machine-readable boundary status — the stop-condition oracle for agent loops.

A ralph-style loop reads `nightward status --json` and only emits its completion
promise when {"boundary": "intact"}.
"""
from __future__ import annotations


def status_payload(report: dict | None, *, stale: bool = False) -> dict:
    """stale=True means the baseline changed since this report; don't trust its verdict."""
    if report is None:
        return {"boundary": "unknown", "unapproved": 0, "changes": [], "judged_same": [],
                "stale": False, "generated_at": None, "judge": None}

    # Every field of a change except its (possibly large) diff, so judged
    # rulings (judged / judge_model / judge_reason) reach agents and CI.
    changes = [
        {k: v for k, v in it.items() if k != "diff"}
        for items in report.get("blast_radius", {}).values()
        for it in items
    ]
    judged_same = [
        {k: it.get(k) for k in ("name", "group", "judge_model", "judge_reason")}
        for it in report.get("judged_same", [])
    ]
    return {
        "boundary": report.get("boundary", "unknown"),
        "unapproved": report.get("unapproved", 0),
        "changes": changes,
        "judged_same": judged_same,
        "stale": stale,
        "generated_at": report.get("generated_at"),
        "judge": report.get("judge"),
    }
