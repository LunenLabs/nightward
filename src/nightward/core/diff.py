"""Compare this run's behaviors against the approved baseline."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

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


# The verdict never waits on a diff: rendering is bounded so huge payloads stay
# near-linear (R1-FIN-02). difflib is quadratic on long runs of repeated lines
# ("},", "{"), so it only ever sees the changed middle, and only up to a budget.
MAX_DIFF_LINES = 2000   # rendered lines kept per behavior (then a truncation marker)
_MATCH_BUDGET = 2000    # changed-middle lines per side difflib may align
_CONTEXT = 3

# Multi-line strings render as an indented block, one source line per diff line,
# so a one-cell change in an HTML/manifest body is one -/+ pair (R1-WEB-04).
_ML_TOKEN = "\x00nightward-ml-{}\x00"
_ML_FOUND = re.compile(r'"\\u0000nightward-ml-(\d+)\\u0000"')


def _swap_multiline(value: Any, found: list[str]) -> Any:
    if isinstance(value, str) and "\n" in value:
        found.append(value)
        return _ML_TOKEN.format(len(found) - 1)
    if isinstance(value, dict):
        return {k: _swap_multiline(v, found) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap_multiline(v, found) for v in value]
    return value


def _display_lines(payload: Any) -> list[str]:
    """canonical_json lines, with each multi-line string expanded into a block:

        "body": \"\"\"
          <line 1>
          <line 2>
        \"\"\",
    """
    found: list[str] = []
    swapped = _swap_multiline(payload, found)
    text = canonical_json(swapped)
    if not found or _ML_FOUND.search(canonical_json(payload)):
        return canonical_json(payload).splitlines()
    out: list[str] = []
    for line in text.splitlines():
        m = _ML_FOUND.search(line)
        if m is None:
            out.append(line)
            continue
        indent = " " * (len(line) - len(line.lstrip(" ")))
        out.append(line[:m.start()] + '"""')
        out.extend(f"{indent}  {part}".replace("\r", "\\r")
                   for part in found[int(m.group(1))].split("\n"))
        out.append(indent + '"""' + line[m.end():])
    return out


def _range(start: int, length: int) -> str:
    # unified-diff range, as difflib formats it
    if length == 1:
        return str(start + 1)
    if length == 0:
        return f"{start},0"
    return f"{start + 1},{length}"


class _Opcodes(difflib.SequenceMatcher):
    """Feed precomputed opcodes to difflib's hunk grouping."""

    def __init__(self, opcodes: list[tuple[str, int, int, int, int]]):
        self._codes = opcodes

    def get_opcodes(self):
        return self._codes


def _positional_opcodes(a: list[str], b: list[str]) -> list[tuple[str, int, int, int, int]]:
    """Line i vs line i - linear; exact when edits don't shift lines."""
    codes: list[tuple[str, int, int, int, int]] = []
    for i, (x, y) in enumerate(zip(a, b, strict=True)):
        tag = "equal" if x == y else "replace"
        if codes and codes[-1][0] == tag:
            codes[-1] = (tag, codes[-1][1], i + 1, codes[-1][3], i + 1)
        else:
            codes.append((tag, i, i + 1, i, i + 1))
    return codes


def _line_diff(a: list[str], b: list[str], n: int = _CONTEXT) -> list[str]:
    """Unified diff of two line lists, bounded in time and output size."""
    if a == b:
        return []
    lo = 0
    while lo < len(a) and lo < len(b) and a[lo] == b[lo]:
        lo += 1
    hi = 0
    while hi < len(a) - lo and hi < len(b) - lo and a[-1 - hi] == b[-1 - hi]:
        hi += 1
    # Window = changed middle plus context; hunk ranges are shifted by `start`.
    start = max(0, lo - n)
    a_win = a[start:len(a) - hi + min(hi, n)]
    b_win = b[start:len(b) - hi + min(hi, n)]
    if max(len(a), len(b)) - lo - hi <= _MATCH_BUDGET:
        groups = difflib.SequenceMatcher(None, a_win, b_win).get_grouped_opcodes(n)
        note = None
    elif len(a_win) == len(b_win):
        groups = _Opcodes(_positional_opcodes(a_win, b_win)).get_grouped_opcodes(n)
        note = "(large change: lines compared by position)"
    else:
        groups = [[("replace", 0, len(a_win), 0, len(b_win))]]
        note = "(large change: too big to align line by line)"
    out = ["--- approved", "+++ received"]
    for group in groups:
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        out.append(f"@@ -{_range(start + i1, i2 - i1)} +{_range(start + j1, j2 - j1)} @@"
                   + (f" {note}" if note else ""))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend(" " + ln for ln in a_win[i1:i2])
                continue
            out.extend("-" + ln for ln in a_win[i1:i2])
            out.extend("+" + ln for ln in b_win[j1:j2])
        if len(out) > MAX_DIFF_LINES:
            break
    if len(out) > MAX_DIFF_LINES:
        out = out[:MAX_DIFF_LINES]
        out.append(f"... diff truncated at {MAX_DIFF_LINES} lines - the payload is too "
                   f"large to show in full; inspect the pending/baseline files")
    return out


def _text_diff(old: Behavior | None, new: Behavior | None) -> str:
    old_lines = _display_lines(old.payload) if old else []
    new_lines = _display_lines(new.payload) if new else []
    if old and new and old_lines == new_lines:
        # the block rendering hid the difference: fall back to raw JSON lines
        old_lines = canonical_json(old.payload).splitlines()
        new_lines = canonical_json(new.payload).splitlines()
    return "\n".join(_line_diff(old_lines, new_lines))


def compare(baseline: dict[str, Behavior], pending: dict[str, Behavior],
            judge=None, *, with_diff: bool = True) -> list[Change]:
    """Fingerprint comparison; optionally soften semantic=True mismatches via a judge.

    The judge only ever turns CHANGED into UNCHANGED-by-meaning (recorded as
    judged=True for audit). It never touches NEW/REMOVED, runs only when both
    the approved baseline and the capture are semantic (flipping the flag is
    itself a CHANGED), and a judge failure keeps the CHANGED verdict —
    the gate fails closed. with_diff=False skips rendering diffs (verdicts only).
    """
    diff = _text_diff if with_diff else (lambda old, new: "")
    changes: list[Change] = []
    for name in sorted(set(baseline) | set(pending)):
        b = baseline.get(name)
        p = pending.get(name)
        if b is None:
            changes.append(Change(name, NEW, group=p.group, diff_text=diff(None, p)))
        elif p is None:
            changes.append(Change(name, REMOVED, group=b.group, diff_text=diff(b, None)))
        elif (old_fp := b.fingerprint()) == (new_fp := p.fingerprint()):
            # Same output, but moved to another feature or switched between
            # exact and judged comparison: both change how the behavior is
            # gated, so they need approval like any other change.
            moves = []
            if b.group != p.group:
                moves.append(f"group: {b.group!r} -> {p.group!r}")
            if b.semantic != p.semantic:
                moves.append(f"semantic: {b.semantic} -> {p.semantic}")
            if moves:
                changes.append(Change(name, CHANGED, group=p.group, diff_text="\n".join(moves)))
            else:
                changes.append(Change(name, UNCHANGED, group=b.group))
        else:
            text = diff(b, p)
            if b.semantic != p.semantic:
                text = f"semantic: {b.semantic} -> {p.semantic}\n{text}"
            change = Change(name, CHANGED, group=p.group, diff_text=text)
            # Judge only what was APPROVED as semantic: turning semantic=True on
            # in a test must not open lenient comparison without approval (D14).
            if judge is not None and b.semantic and p.semantic:
                verdict = judge.equivalent(b.payload, p.payload, old_fp, new_fp, name=name)
                if verdict is not None:
                    change.judged = True
                    change.judge_model = verdict.model
                    change.judge_reason = verdict.reason
                    if verdict.verdict == SAME:
                        change.kind = UNCHANGED
            changes.append(change)
    return changes
