"""Machine-readable boundary status — the stop-condition oracle for agent loops.

A ralph-style loop reads `nightward status --json` and only emits its completion
promise when {"boundary": "intact"}.
"""
from __future__ import annotations


def status_payload(report: dict | None, *, stale: bool = False) -> dict:
    """stale=True means the baseline changed since this report; don't trust its verdict."""
    if report is None:
        return {"boundary": "unknown", "unapproved": 0, "changes": [],
                "stale": False, "generated_at": None}

    changes = [
        {"name": it["name"], "kind": it["kind"], "group": it.get("group")}
        for items in report.get("blast_radius", {}).values()
        for it in items
    ]
    return {
        "boundary": report.get("boundary", "unknown"),
        "unapproved": report.get("unapproved", 0),
        "changes": changes,
        "stale": stale,
        "generated_at": report.get("generated_at"),
    }
