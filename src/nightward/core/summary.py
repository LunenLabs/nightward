"""Condense a change into what moved where (`nightward review --summary`).

A 59 KB diff gets skimmed, and an approval nobody read is an automatic one.
The summary walks both payloads, folds list indices (`cases[3].correct` ->
`cases[*].correct`) and counts, per path, which old value became which new
one: `cases[*].correct  78/80 changed  "OK" -> 41 distinct values`. A NEW
or REMOVED behavior has nothing to compare, so its summary is the value
distribution per path instead. Display only: fingerprints, verdicts and the
store never pass through here.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

ROOT = "$"
ABSENT = "(absent)"
_VALUE_CHARS = 60


@dataclass
class PathSummary:
    path: str
    total: int = 0                                  # occurrences compared at this path
    pairs: Counter = field(default_factory=Counter)  # (old, new) display strings -> count

    @property
    def changed(self) -> int:
        return sum(self.pairs.values())


def _show(v: Any) -> str:
    if isinstance(v, list) and v:
        return f"[{len(v)} items]"
    if isinstance(v, dict) and v:
        return f"{{{len(v)} keys}}"
    text = json.dumps(v, ensure_ascii=False)
    return text if len(text) <= _VALUE_CHARS else text[:_VALUE_CHARS - 3] + "..."


def _show_pair(old: Any, new: Any) -> tuple[str, str]:
    """Both sides, cut so the difference stays visible: two long strings that
    share their first 60 characters must not read as `"abc..." -> "abc..."`."""
    o, n = _show(old), _show(new)
    if o != n or not (isinstance(old, str) and isinstance(new, str)):
        return o, n
    a, b = json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False)
    at = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y),
              min(len(a), len(b)))
    start = max(0, at - 20)

    def window(text: str) -> str:
        cut = text[start:start + _VALUE_CHARS - 6]
        return (("..." if start else "") + cut
                + ("..." if start + len(cut) < len(text) else ""))
    return window(a), window(b)


def _child(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _item(prefix: str) -> str:
    return f"{prefix or ROOT}[*]"


def _get(out: dict[str, PathSummary], path: str) -> PathSummary:
    return out.setdefault(path, PathSummary(path or ROOT))


def _compare(old: Any, new: Any, prefix: str, out: dict[str, PathSummary]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            path = _child(prefix, k)
            if k in old and k in new:
                _compare(old[k], new[k], path, out)
            else:
                s = _get(out, path)
                s.total += 1
                s.pairs[(_show(old[k]) if k in old else ABSENT,
                         _show(new[k]) if k in new else ABSENT)] += 1
        return
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for o, n in zip(old, new, strict=True):
            _compare(o, n, _item(prefix), out)
        return
    # A leaf, a type change, or a list whose length changed: element-by-element
    # pairing would shift every item after an insertion, so the list is one value.
    s = _get(out, prefix)
    s.total += 1
    if json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True):
        s.pairs[_show_pair(old, new)] += 1


def _describe(value: Any, prefix: str, out: dict[str, Counter]) -> None:
    if isinstance(value, dict) and value:
        for k in sorted(value):
            _describe(value[k], _child(prefix, k), out)
    elif isinstance(value, list) and value:
        for v in value:
            _describe(v, _item(prefix), out)
    else:
        out.setdefault(prefix or ROOT, Counter())[_show(value)] += 1


def changes(old: Any, new: Any) -> list[PathSummary]:
    """Paths whose values differ, most-changed first."""
    out: dict[str, PathSummary] = {}
    _compare(old, new, "", out)
    moved = [s for s in out.values() if s.pairs]
    return sorted(moved, key=lambda s: (-s.changed, s.path))


def distribution(value: Any) -> list[tuple[str, Counter]]:
    """Every leaf path of one payload with its values counted, most values first."""
    out: dict[str, Counter] = {}
    _describe(value, "", out)
    return sorted(out.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))


# ---- list order ---------------------------------------------------------------
# Folding indices into [*] drops the order, and for a ranking the order is what
# a reviewer has to see. Short lists get an order row; longer ones are read as
# a diff (`--max-lines 0`).
_ORDER_MAX = 20
_LABEL_CHARS = 24


def _lists(value: Any, prefix: str, out: dict[str, list]) -> None:
    """Lists reachable through dict keys (not inside other lists: their paths
    would fold together), by summary path."""
    if isinstance(value, dict):
        for k in sorted(value):
            _lists(value[k], _child(prefix, k), out)
    elif isinstance(value, list):
        out[prefix or ROOT] = value


def _label(item: Any) -> str | None:
    """What names a list item: a scalar is itself; a dict, its first string
    value in key order (the order the baseline file is written in)."""
    if isinstance(item, dict):
        text = next((v for _, v in sorted(item.items()) if isinstance(v, str)), None)
    elif isinstance(item, list):
        return None
    else:
        text = item if isinstance(item, str) else json.dumps(item)
    if text is None:
        return None
    return text if len(text) <= _LABEL_CHARS else text[:_LABEL_CHARS - 3] + "..."


def _labels(items: list) -> list[str] | None:
    labels = [_label(i) for i in items]
    return None if None in labels else labels


def _old_positions(old: list, new: list) -> list[str]:
    """Each new item as the old position it came from (#i): a reorder of items
    with no label."""
    free: dict[str, list[int]] = {}
    for i, item in enumerate(old):
        free.setdefault(json.dumps(item, sort_keys=True), []).append(i)
    return [f"#{free[json.dumps(item, sort_keys=True)].pop(0)}" for item in new]


def order_rows(old: Any, new: Any) -> tuple[list[tuple[str, str]], set[str]]:
    """(path, line) rows that show the order of short lists, and the list paths
    whose per-field rows only repeat a pure reorder (hide them: `is_hidden`).

    One payload (NEW/REMOVED): `order: A, B, C`. Two: `reordered: A, B -> B, A`
    when the same items moved, `order: ... -> ...` when the labels' order
    changed along with values.
    """
    if old is None or new is None:
        found: dict[str, list] = {}
        _lists(new if old is None else old, "", found)
        return ([(path, "order: " + ", ".join(labels)) for path, items in sorted(found.items())
                 if 1 < len(items) <= _ORDER_MAX and (labels := _labels(items))], set())
    before: dict[str, list] = {}
    after: dict[str, list] = {}
    _lists(old, "", before)
    _lists(new, "", after)
    rows, hidden = [], set()
    for path in sorted(set(before) & set(after)):
        o, n = before[path], after[path]
        if not (1 < len(n) <= _ORDER_MAX and 1 < len(o) <= _ORDER_MAX):
            continue
        canon_o = [json.dumps(i, sort_keys=True) for i in o]
        canon_n = [json.dumps(i, sort_keys=True) for i in n]
        if canon_o == canon_n:
            continue
        lo, ln = _labels(o), _labels(n)
        if sorted(canon_o) == sorted(canon_n):
            if lo is None or ln is None:
                lo, ln = [f"#{i}" for i in range(len(o))], _old_positions(o, n)
            rows.append((path, f"reordered: {', '.join(lo)} -> {', '.join(ln)}"))
            hidden.add(path)
        elif lo is not None and ln is not None and lo != ln:
            rows.append((path, f"order: {', '.join(lo)} -> {', '.join(ln)}"))
    return rows, hidden


def is_hidden(path: str, hidden: set[str]) -> bool:
    """Whether a `changes()` path lies inside a list that only reordered."""
    return any(path == h or path.startswith(f"{h}[*]") for h in hidden)


def _counted(values: Counter, limit: int) -> str:
    shown = [v if n == 1 else f"{v} x{n}" for v, n in values.most_common(limit)]
    more = len(values) - limit
    return ", ".join(shown) + (f", +{more} more" if more > 0 else "")


def change_line(s: PathSummary, limit: int = 3) -> str:
    """`old -> new` for one path, collapsing whichever side has a single value."""
    olds, news = Counter(), Counter()
    for (o, n), c in s.pairs.items():
        olds[o] += c
        news[n] += c
    if len(s.pairs) == 1:
        (o, n), c = next(iter(s.pairs.items()))
        return f"{o} -> {n}" + (f" (x{c})" if c > 1 else "")
    if len(olds) == 1:
        return f"{next(iter(olds))} -> {len(news)} distinct values: {_counted(news, limit)}"
    if len(news) == 1:
        return f"{len(olds)} distinct values: {_counted(olds, limit)} -> {next(iter(news))}"
    shown = [f"{o} -> {n}" + (f" (x{c})" if c > 1 else "")
             for (o, n), c in s.pairs.most_common(limit)]
    more = len(s.pairs) - limit
    return "; ".join(shown) + (f"; +{more} more pair(s)" if more > 0 else "")


def distribution_line(values: Counter, limit: int = 3) -> str:
    total = sum(values.values())
    if len(values) == 1:
        v = next(iter(values))
        return v if total == 1 else f"{v} (all {total})"
    return f"{total} values, {len(values)} distinct: {_counted(values, limit)}"
