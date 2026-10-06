"""Explain the drift behind CHANGED behaviors; call it volatile only on evidence.

A noisy gate is a dead gate, but a scrub rule that masks a real change is
worse: the gate goes blind there for good. One before/after pair is no
evidence of volatility, so doctor calls a value volatile only when the values
themselves show it (a date-time, a Unix epoch under a time-like key, a random
token behind a stable prefix), and then suggests the narrowest rule that hides
exactly that drift: a value-shaped regex, or a field mask for a key that is
stable nowhere else. Float noise and order-only list changes get capture-time
fixes, and everything else "looks like a real change". Strictly read-only: it
never edits the store, applies a rule, or moves the boundary - taming noise
stays a human decision, exactly like approve.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

from .behavior import Behavior
from .visible import reveal

ROOT = "$"

VOLATILE = "volatile"      # varies by design, with evidence -> may get a scrub rule
FLOAT = "float-noise"      # same float up to the last digits -> round before capture
ORDER = "order-only"       # same elements, new order -> sort before capture
CONTENT = "content-hash"   # a hash of content changed -> the content changed
REAL = "changed"           # no sign of volatility -> looks like a real change
STRUCTURAL = "structural"  # keys / list length / value type changed

_INDEX = re.compile(r"\[\d+\]")
# Value shapes that change on every run. A match never leaves a JSON string,
# so the shape itself is a safe scrub.register pattern.
_TIME_SHAPES = (
    ("HTTP date",
     r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct"
     r"|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} (?:GMT|[+-]\d{4})", "<HTTP_DATE>"),
    ("date-time",
     r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", "<DATETIME>"),
)
_TIME_KEY = re.compile(
    r"(?:^|[_.-])(?:created|updated|modified|expires?|issued|time|timestamp|ts|date|epoch"
    r"|at|iat|exp|nbf)$|(?:At|Time|Ts|Date)$", re.IGNORECASE)
_HASH_KEY = re.compile(r"(?:sha\d*|md5|hash|digest|checksum|crc\d*)$", re.IGNORECASE)
_EPOCH_RANGES = ((1_000_000_000, 4_102_444_800),               # seconds, 2001..2100
                 (1_000_000_000_000, 4_102_444_800_000))       # milliseconds
_HASH_LENGTHS = {32, 40, 64, 128}
_CONTEXT = 16  # chars of literal context that anchor a token rule


def _finding(path: str, kind: str, note: str, rule: str | None = None,
             detail: str = "") -> dict:
    return {"path": path, "kind": kind, "note": note, "rule": rule, "detail": detail}


def _type(v: Any) -> str:
    return "null" if v is None else type(v).__name__


def _short(v: Any, limit: int = 40) -> str:
    text = json.dumps(v, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _json_inner(s: str) -> str:
    """How `s` appears inside a JSON string in canonical_json text."""
    return json.dumps(s, ensure_ascii=False)[1:-1]


def _is_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _is_random(tok: str) -> bool:
    return (len(tok) >= 8 and tok.isascii() and tok.isalnum()
            and any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok))


def _hides(pattern: str, old: str, new: str) -> bool:
    """Self-check: the pattern, applied to both values as JSON text, equalizes them."""
    pat = re.compile(pattern)
    return pat.sub("", f'"{_json_inner(old)}"') == pat.sub("", f'"{_json_inner(new)}"')


def _time_rule(old: str, new: str) -> tuple[str, str] | None:
    for label, shape, placeholder in _TIME_SHAPES:
        if re.search(shape, old) and re.search(shape, new) and _hides(shape, old, new):
            return f"{label} changes on every run", \
                f"scrub.register({shape!r}, {placeholder!r})"
    return None


def _token_rule(old: str, new: str) -> tuple[str, str] | None:
    """A random token behind stable literal context -> a context-anchored regex."""
    limit = min(len(old), len(new))
    p = 0
    while p < limit and old[p] == new[p]:
        p += 1
    s = 0
    while s < limit - p and old[-1 - s] == new[-1 - s]:
        s += 1
    start = p
    while start > 0 and _is_alnum(old[start - 1]):      # a shared head of the token
        start -= 1
    end_o, end_n = len(old) - s, len(new) - s
    while end_o < len(old) and _is_alnum(old[end_o]):   # a shared tail of the token
        end_o, end_n = end_o + 1, end_n + 1
    tok_o, tok_n = old[start:end_o], new[start:end_n]
    if not (_is_random(tok_o) and _is_random(tok_n)):
        return None
    before, after = old[max(0, start - _CONTEXT):start], old[end_o:end_o + 1]
    if not before and not after and len(tok_o) in _HASH_LENGTHS:
        return None  # a bare hash-sized value: likely a content hash, not noise
    hexes = "0123456789abcdef"
    if all(c in hexes for c in tok_o + tok_n):
        cls = "[0-9a-f]"
    elif all(c in hexes.upper() for c in tok_o + tok_n):
        cls = "[0-9A-F]"
    else:
        cls = "[0-9A-Za-z]"
    lo, hi = sorted((len(tok_o), len(tok_n)))
    size = f"{{{lo}}}" if lo == hi else f"{{{lo},{hi}}}"
    behind = re.escape(_json_inner(before)) if before else '"'
    ahead = re.escape(_json_inner(after)) if after else '"'
    pattern = f"(?<={behind}){cls}{size}(?={ahead})"
    if not _hides(pattern, old, new):
        return None
    where = f"after {before[-12:]!r}" if before else "as the whole value"
    return f"random token {where}", f"scrub.register({pattern!r}, '<TOKEN>')"


def _classify(path: str, key: str | None, old: Any, new: Any) -> dict:
    detail = f"{_short(old)} -> {_short(new)}"
    if key is not None and _HASH_KEY.search(key):
        return _finding(path, CONTENT, "content hash changed, so the content changed - "
                        "review it; never scrub a hash", detail=detail)
    if isinstance(old, str):
        hit = _time_rule(old, new) or _token_rule(old, new)
        if hit:
            return _finding(path, VOLATILE, hit[0], hit[1], detail)
    elif isinstance(old, float) and math.isclose(old, new, rel_tol=1e-9, abs_tol=0.0):
        return _finding(path, FLOAT, "float noise in the last digits: round it before "
                        "capturing, e.g. round(value, 10) - don't mask it", detail=detail)
    elif (isinstance(old, int) and not isinstance(old, bool) and key is not None
          and _TIME_KEY.search(key)
          and any(lo <= old <= hi and lo <= new <= hi for lo, hi in _EPOCH_RANGES)):
        return _finding(path, VOLATILE, "Unix timestamp",
                        f'scrub.register_field("{key}")', detail)
    if isinstance(old, str) and isinstance(new, str) and (shown := reveal(old, new)):
        # Both sides print the same: show and name what differs (R2-FIN-04).
        return _finding(path, REAL, f"looks like a real change - invisible or look-alike "
                        f"characters: {shown[2]}",
                        detail=f'"{_clip(shown[0])}" -> "{_clip(shown[1])}"')
    return _finding(path, REAL, "looks like a real change", detail=detail)


def _clip(escaped: str, limit: int = 60) -> str:
    """A window of `escaped` around its first escaped character."""
    if len(escaped) <= limit:
        return escaped
    at = max(0, escaped.find("\\") - 20)
    return (("..." if at else "") + escaped[at:at + limit]
            + ("..." if at + limit < len(escaped) else ""))


def _sortable(v: Any) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def _walk(old: Any, new: Any, prefix: str, key: str | None, out: list[dict]) -> None:
    # `key` is the dict key holding this value directly (None inside a list),
    # so a field mask is only ever offered for the scalar itself, never for
    # the list or record that contains the drift (R1-WEB-02).
    path = prefix or ROOT
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            child = f"{prefix}.{k}" if prefix else k
            if k not in old or k not in new:
                out.append(_finding(child, STRUCTURAL,
                                    "key added" if k in new else "key removed"))
            else:
                _walk(old[k], new[k], child, k, out)
        return
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.append(_finding(f"{path}[]", STRUCTURAL,
                                f"list length {len(old)} -> {len(new)}"))
            return
        if old != new and sorted(map(_sortable, old)) == sorted(map(_sortable, new)):
            out.append(_finding(path, ORDER, "same elements in a new order: sort the list "
                                "before capturing (e.g. sorted(...)) - don't mask it"))
            return
        for i, (o, n) in enumerate(zip(old, new, strict=True)):
            _walk(o, n, f"{prefix}[{i}]" if prefix else f"{ROOT}[{i}]", None, out)
        return
    if type(old) is not type(new):
        out.append(_finding(path, STRUCTURAL, f"type {_type(old)} -> {_type(new)}",
                            detail=f"{_short(old)} -> {_short(new)}"))
    elif old != new:
        out.append(_classify(path, key, old, new))


def findings(old: Any, new: Any) -> list[dict]:
    """Every leaf-level difference between two payloads, classified."""
    out: list[dict] = []
    _walk(old, new, "", None, out)
    return out


def _keys(payload: Any) -> Iterator[str]:
    if isinstance(payload, dict):
        for k, v in payload.items():
            yield k
            yield from _keys(v)
    elif isinstance(payload, list):
        for v in payload:
            yield from _keys(v)


def _collapse(found: list[dict]) -> list[dict]:
    """Merge findings that differ only by list index ($[0].x, $[1].x -> $[*].x)."""
    merged: dict[tuple, dict] = {}
    for f in found:
        group = (_INDEX.sub("[*]", f["path"]), f["kind"], f["note"], f["rule"])
        if group in merged:
            merged[group]["count"] += 1
            merged[group]["path"] = group[0]
            merged[group]["detail"] = ""
        else:
            merged[group] = f | {"count": 1}
    return list(merged.values())


def _withhold_colliding_field_rules(raw: dict[str, list[dict]],
                                    pending: dict[str, Behavior]) -> None:
    # register_field masks the key in EVERY behavior. Withhold it when the key
    # also occurs somewhere it is not volatile (R1-LLM-09: "id" in rag.hits).
    volatile_hits: dict[str, Counter] = {}
    for name, found in raw.items():
        for f in found:
            if f["rule"] and f["rule"].startswith("scrub.register_field("):
                volatile_hits.setdefault(f["rule"], Counter())[name] += 1
    for rule, hits in volatile_hits.items():
        key = rule.split('"')[1]
        stable = sorted(name for name, b in pending.items()
                        if sum(k == key for k in _keys(b.payload)) > hits[name])
        if not stable:
            continue
        shown = ", ".join(stable[:3]) + (" ..." if len(stable) > 3 else "")
        for found in raw.values():
            for f in found:
                if f["rule"] == rule:
                    f["rule"] = None
                    f["note"] += (f"; no field rule: \"{key}\" is stable in {shown} - "
                                  f"drop or mask it at capture time in this test instead")


def diagnose(baseline: dict[str, Behavior], pending: dict[str, Behavior],
             only: set[str] | None = None) -> dict:
    """Per-behavior classified drift + scrub suggestions backed by evidence.

    only: diagnose just these names. The field-rule collision check still sees
    every pending behavior, so scoping never widens a suggestion.
    """
    # Fingerprints only: compare() would also build a text diff per change,
    # which doctor never shows and which is slow on big payloads.
    changed = [name for name in sorted(set(baseline) & set(pending))
               if (only is None or name in only)
               and baseline[name].fingerprint() != pending[name].fingerprint()]
    raw = {name: findings(baseline[name].payload, pending[name].payload)
           for name in changed}
    _withhold_colliding_field_rules(raw, pending)
    suggestions: dict[str, dict] = {}
    for name, found in raw.items():
        for f in found:
            if f["rule"]:
                s = suggestions.setdefault(
                    f["rule"], {"rule": f["rule"], "reason": f["note"], "behaviors": []})
                if name not in s["behaviors"]:
                    s["behaviors"].append(name)
    return {"changed": changed,
            "behaviors": {name: _collapse(found) for name, found in raw.items()},
            "suggestions": list(suggestions.values())}
