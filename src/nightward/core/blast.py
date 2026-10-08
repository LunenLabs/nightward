"""Aggregate raw changes into a blast-radius report.

This is the layer that sets nightward apart from a plain snapshot library:
"AI touched A → here are the N behaviors that moved, grouped by feature."
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .diff import CHANGED, NEW, NOT_RUN, REMOVED, UNCHANGED, Change


def aggregate(changes: list[Change]) -> dict:
    unapproved = [c for c in changes if c.kind not in (UNCHANGED, NOT_RUN)]

    by_group: dict[str, list[dict]] = defaultdict(list)
    for c in unapproved:
        by_group[c.group or "(ungrouped)"].append(c.to_dict() | {"diff": c.diff_text})

    by_kind = Counter(c.kind for c in changes)
    counts = {
        "total": len(changes),
        "unchanged": by_kind[UNCHANGED],
        "new": by_kind[NEW],
        "changed": by_kind[CHANGED],
        "removed": by_kind[REMOVED],
        "not_run": by_kind[NOT_RUN],
        # fingerprint mismatches an LLM judge ruled equivalent (audit visibility)
        "judged_same": sum(1 for c in changes if c.kind == UNCHANGED and c.judged),
    }

    return {
        "boundary": "intact" if not unapproved else "breached",
        "unapproved": len(unapproved),
        "counts": counts,
        "blast_radius": {g: items for g, items in sorted(by_group.items())},
        # Not in the boundary, but listed with their diffs so a reviewer can
        # audit what the judge waved through (R1-LLM-04).
        "judged_same": [c.to_dict() | {"diff": c.diff_text}
                        for c in changes if c.kind == UNCHANGED and c.judged],
        # Approved behaviors this run did not check: their test was deselected.
        "not_run": [{"name": c.name, "group": c.group} for c in changes if c.kind == NOT_RUN],
    }
