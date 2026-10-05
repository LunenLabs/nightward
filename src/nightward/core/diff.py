"""Compare this run's behaviors against the approved baseline."""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from .behavior import Behavior, canonical_json

NEW = "NEW"
CHANGED = "CHANGED"
REMOVED = "REMOVED"
UNCHANGED = "UNCHANGED"

SAME = "SAME"  # judge verdict that collapses CHANGED into UNCHANGED (see judge.py)


@dataclass
class Change:
    name: str
    kind: str
    group: str | None = None
    diff_text: str = ""
    judged: bool = False      # an LLM judge ruled on this fingerprint mismatch
    judge_model: str = ""     # provider:model spec that ruled
    judge_reason: str = ""

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "group": self.group}
        if self.judged:
            d |= {"judged": True, "judge_model": self.judge_model,
                  "judge_reason": self.judge_reason}
        return d


def _text_diff(old: Behavior | None, new: Behavior | None) -> str:
    old_lines = canonical_json(old.payload).splitlines() if old else []
    new_lines = canonical_json(new.payload).splitlines() if new else []
    return "\n".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile="approved", tofile="received", lineterm=""
        )
    )


def compare(baseline: dict[str, Behavior], pending: dict[str, Behavior],
            judge=None) -> list[Change]:
    """Fingerprint comparison; optionally soften semantic=True mismatches via a judge.

    The judge only ever turns CHANGED into UNCHANGED-by-meaning (recorded as
    judged=True for audit). It never touches NEW/REMOVED, never runs on
    deterministic behaviors, and a judge failure keeps the CHANGED verdict —
    the gate fails closed.
    """
    changes: list[Change] = []
    for name in sorted(set(baseline) | set(pending)):
        b = baseline.get(name)
        p = pending.get(name)
        if b is None:
            changes.append(Change(name, NEW, group=p.group, diff_text=_text_diff(None, p)))
        elif p is None:
            changes.append(Change(name, REMOVED, group=b.group, diff_text=_text_diff(b, None)))
        elif (old_fp := b.fingerprint()) == (new_fp := p.fingerprint()):
            if b.group != p.group:
                # Same output, moved to another feature: the blast radius would
                # keep pointing at the old group until someone approves the move.
                changes.append(Change(name, CHANGED, group=p.group,
                                      diff_text=f"group: {b.group!r} -> {p.group!r}"))
            else:
                changes.append(Change(name, UNCHANGED, group=b.group))
        else:
            change = Change(name, CHANGED, group=p.group, diff_text=_text_diff(b, p))
            if judge is not None and p.semantic:
                verdict = judge.equivalent(b.payload, p.payload, old_fp, new_fp, name=name)
                if verdict is not None:
                    change.judged = True
                    change.judge_model = verdict.model
                    change.judge_reason = verdict.reason
                    if verdict.verdict == SAME:
                        change.kind = UNCHANGED
            changes.append(change)
    return changes
