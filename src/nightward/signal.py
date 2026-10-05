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
        return {"boundary": "unknown", "unapproved": 0, "changes": [],
                "stale": False, "incomplete": None, "generated_at": None}

    changes = [
        {"name": it["name"], "kind": it["kind"], "group": it.get("group")}
        for items in report.get("blast_radius", {}).values()
        for it in items
    ]
    boundary = report.get("boundary", "unknown")
    incomplete = report.get("incomplete")
    if stale:
        boundary = "stale"
    elif incomplete and boundary == "intact":
        boundary = "incomplete"   # a failing capture test is never "done"
    return {
        "boundary": boundary,
        "unapproved": report.get("unapproved", 0),
        "changes": changes,
        "stale": stale,
        "incomplete": incomplete,
        "generated_at": report.get("generated_at"),
    }
