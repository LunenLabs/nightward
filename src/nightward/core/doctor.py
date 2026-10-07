"""Explain the drift behind CHANGED behaviors; call it volatile only on evidence.

A noisy gate is a dead gate, but a scrub rule that masks a real change is
worse: the gate goes blind there for good. One before/after pair is no
evidence of volatility, so doctor calls a value volatile only when the value
itself shows it (a random token behind a stable prefix) and then suggests the
narrowest rule that hides exactly that drift. Dates, Unix timestamps and
reordered lists "look like a real change" first (a deadline or an event order
can be the product), with a remedy offered only "if it is not part of the
contract". Float noise (a few ULPs) gets a rounding fix at capture time.
Every suggested rule is scoped to the field or context that drifted, verified
to equalize both samples, and withheld when it would also hit a stable value
elsewhere in the capture (D15). Strictly read-only: it never edits the store,
applies a rule, or moves the boundary - taming noise stays a human decision,
exactly like approve.
"""
from __future__ import annotations

import datetime
import email.utils
import json
import math
import posixpath
import re
import struct
from collections import Counter
from collections.abc import Iterator
from typing import Any

from .behavior import Behavior, canonical_json
from .visible import reveal

ROOT = "$"

VOLATILE = "volatile"      # random by evidence -> may get a scrub rule
DATE = "date"              # a date/timestamp moved -> real first, rule only if not contract
FLOAT = "float-noise"      # same float up to a few ULPs -> round before capture
ORDER = "order-only"       # same elements, new order -> real first, else sort
CONTENT = "content-hash"   # a hash of content changed -> the content changed
REAL = "changed"           # no sign of volatility -> looks like a real change
STRUCTURAL = "structural"  # keys / list length / value type changed

_INDEX = re.compile(r"\[\d+\]")
_TIME_SHAPES = (
    ("HTTP date",
     r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct"
     r"|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} (?:GMT|[+-]\d{4})"),
    ("date-time",
     r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
)
_TIME_KEY = re.compile(
    r"(?:^|[_.-])(?:created|updated|modified|expires?|issued|time|timestamp|ts|date|epoch"
    r"|at|iat|exp|nbf|reset|start)$|(?:At|Time|Ts|Date)$", re.IGNORECASE)
_HASH_KEY = re.compile(r"(?:sha\d*|md5|hash|digest|checksum|crc\d*)$", re.IGNORECASE)
_EPOCH_RANGES = ((1_000_000_000, 4_102_444_800),               # seconds, 2001..2100
                 (1_000_000_000_000, 4_102_444_800_000))       # milliseconds
_HASH_LENGTHS = {32, 40, 64, 128}
_CONTEXT = 16      # chars of literal context that anchor a substring rule
_ULPS = 4          # float noise: at most this many units in the last place
_ZERO_FLOOR = 1e-12  # ...or this close to zero (cancellation residue)
_IF_NOT_CONTRACT = "if it is not part of the contract"
_REAL = "looks like a real change"


def _finding(path: str, kind: str, note: str, rule: str | None = None, detail: str = "",
             *, conditional: bool = False, match: tuple | None = None,
             values: tuple | None = None) -> dict:
    # match: ("field", key) | ("regex", pattern) - what the rule would touch,
    # for the collision check. values: (old, new) for the date-shift check.
    return {"path": path, "kind": kind, "note": note, "rule": rule, "detail": detail,
            "conditional": conditional, "_match": match, "_values": values}


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


def _field_rule(key: str) -> tuple[str, tuple]:
    return f'scrub.register_field("{key}")', ("field", key)


def _regex_rule(pattern: str, placeholder: str) -> tuple[str, tuple]:
    return f"scrub.register({pattern!r}, {placeholder!r})", ("regex", pattern)


# ---- dates and timestamps: real first ----------------------------------------


def _epoch(v: Any) -> float | None:
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    for lo, hi in _EPOCH_RANGES:
        if lo <= v <= hi:
            return v / 1000 if lo > _EPOCH_RANGES[0][1] else v
    return None


def _as_datetime(v: Any) -> datetime.datetime | None:
    seconds = _epoch(v)
    if seconds is not None:
        return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    if not isinstance(v, str):
        return None
    try:
        return datetime.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(v)
    except (TypeError, ValueError):
        return None


def _date_finding(path: str, key: str | None, old: Any, new: Any, detail: str) -> dict | None:
    if (key is not None and _TIME_KEY.search(key)
            and _epoch(old) is not None and _epoch(new) is not None):
        rule, match = _field_rule(key)
        return _finding(path, DATE, f"Unix timestamp changed - {_REAL}", rule, detail,
                        conditional=True, match=match, values=(old, new))
    if not isinstance(old, str):
        return None
    for label, shape in _TIME_SHAPES:
        if not (re.search(shape, old) and re.search(shape, new) and _hides(shape, old, new)):
            continue
        rule = match = None
        if re.fullmatch(shape, old) and re.fullmatch(shape, new):
            if key is not None:   # the whole value of a named field
                rule, match = _field_rule(key)
        else:                     # inside a longer string: anchor on the text before it
            start = re.search(shape, old).start()
            before = old[max(0, start - _CONTEXT):start]
            pattern = f"(?<={re.escape(_json_inner(before))}){shape}"
            if before and _hides(pattern, old, new):
                rule, match = _regex_rule(pattern, "<DATETIME>")
        note = f"{label} changed - {_REAL}"
        if rule is None:
            note += (f"; {_IF_NOT_CONTRACT}, mask it at capture time in this test"
                     + (" (or capture it in a dict, e.g. dict(headers), so a field rule "
                        "can name it)" if key is None else ""))
        return _finding(path, DATE, note, rule, detail, conditional=True, match=match,
                        values=(old, new))
    return None


def _fmt_delta(delta: datetime.timedelta) -> str:
    seconds = delta.total_seconds()
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds % size == 0:
            n = int(seconds // size)
            return f"{n:+d} {unit}{'' if abs(n) == 1 else 's'}"
    return f"{seconds:+g} seconds"


def _constant_shift(found: list[dict]) -> None:
    """Several dates at one path all moved by the same delta: that is a code
    change (an SLA, an offset), not noise (R2-DATA-02)."""
    groups: dict[str, list[dict]] = {}
    for f in found:
        if f["kind"] == DATE and f["_values"]:
            groups.setdefault(_INDEX.sub("[*]", f["path"]), []).append(f)
    for group in groups.values():
        if len(group) < 2:
            continue
        try:
            deltas = {_as_datetime(f["_values"][1]) - _as_datetime(f["_values"][0])
                      for f in group}
        except TypeError:   # unparsed, or naive vs aware
            continue
        if len(deltas) == 1 and (delta := deltas.pop()):
            for f in group:
                f.update(kind=REAL, rule=None, conditional=False, _match=None,
                         note=f"all {len(group)} values moved by {_fmt_delta(delta)} - "
                              f"{_REAL}")


# ---- random tokens: volatile by evidence -------------------------------------


def _token_rule(old: str, new: str) -> tuple[str, str, tuple] | None:
    """A random token behind stable literal context -> a context-anchored regex
    with a base62 class and an open-ended length (R2-LLM-04): the next id may
    be shorter, longer, or not hex."""
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
    behind = re.escape(_json_inner(before)) if before else '"'
    ahead = re.escape(_json_inner(after)) if after else '"'
    pattern = f"(?<={behind})[0-9A-Za-z]{{8,}}(?={ahead})"
    if not _hides(pattern, old, new):
        return None
    where = f"after {before[-12:]!r}" if before else "as the whole value"
    rule, match = _regex_rule(pattern, "<TOKEN>")
    return f"random token {where}", rule, match


# ---- floats: a few ULPs, never a large delta ---------------------------------


def _is_f32(x: float) -> bool:
    try:
        return struct.unpack("<f", struct.pack("<f", x))[0] == x
    except OverflowError:
        return False


def _ulp32(x: float) -> float:
    return 2.0 ** (math.frexp(x)[1] - 24) if x else 2.0 ** -149


def _float_noise(a: float, b: float) -> str | None:
    """"float64" / "float32" when a and b differ only in their last bits."""
    if a.is_integer() and b.is_integer():
        return None          # counts and amounts: any difference is real
    diff, big = abs(a - b), max(abs(a), abs(b))
    if diff >= 1.0:
        return None          # a large absolute delta is never noise
    if diff <= _ULPS * math.ulp(big) or diff <= _ZERO_FLOOR:
        return "float64"
    if _is_f32(a) and _is_f32(b) and diff <= _ULPS * _ulp32(big):
        return "float32"     # e.g. embeddings via .tolist(), another machine's BLAS
    return None


def _classify(path: str, key: str | None, old: Any, new: Any) -> dict:
    detail = f"{_short(old)} -> {_short(new)}"
    if key is not None and _HASH_KEY.search(key):
        return _finding(path, CONTENT, "content hash changed, so the content changed - "
                        "review it; never scrub a hash", detail=detail)
    date = _date_finding(path, key, old, new, detail)
    if date:
        return date
    if isinstance(old, str):
        hit = _token_rule(old, new)
        if hit:
            return _finding(path, VOLATILE, hit[0], hit[1], detail, match=hit[2])
    elif isinstance(old, float):
        width = _float_noise(old, new)
        if width:
            digits = 6 if width == "float32" else 12
            return _finding(path, FLOAT, f"{width} noise in the last digits: round to "
                            f"{digits} significant digits before capturing, e.g. "
                            f'float(f"{{x:.{digits}g}}") - don\'t mask it', detail=detail)
    if isinstance(old, str) and isinstance(new, str) and (shown := reveal(old, new)):
        # Both sides print the same: show and name what differs (R2-FIN-04).
        return _finding(path, REAL, f"{_REAL} - invisible or look-alike "
                        f"characters: {shown[2]}",
                        detail=f'"{_clip(shown[0])}" -> "{_clip(shown[1])}"')
    return _finding(path, REAL, _REAL, detail=detail)


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
            # Order can be the contract (event or ledger order, rankings): one
            # reorder is no evidence that it is a set (R2-FIN-02).
            out.append(_finding(path, ORDER, f"same elements in a new order - {_REAL} "
                                f"(event, ledger or ranking order); {_IF_NOT_CONTRACT} "
                                f"(e.g. a set), sort it before capturing"))
            return
        for i, (o, n) in enumerate(zip(old, new, strict=True)):
            _walk(o, n, f"{prefix}[{i}]" if prefix else f"{ROOT}[{i}]", None, out)
        return
    if type(old) is not type(new):
        out.append(_finding(path, STRUCTURAL, f"type {_type(old)} -> {_type(new)}",
                            detail=f"{_short(old)} -> {_short(new)}"))
    elif old != new:
        out.append(_classify(path, key, old, new))
    elif isinstance(old, float) and math.copysign(1, old) != math.copysign(1, new):
        # 0.0 == -0.0 in Python, but the fingerprint sees "-0.0" (R2-DATA-01)
        out.append(_finding(path, FLOAT, "sign of zero changed (e.g. round(-0.0001, 2) "
                            "is -0.0): add 0.0 before capturing (x + 0.0 turns -0.0 "
                            "into 0.0)", detail=f"{old!r} -> {new!r}"))


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
        f = {k: v for k, v in f.items() if not k.startswith("_")}
        group = (_INDEX.sub("[*]", f["path"]), f["kind"], f["note"], f["rule"])
        if group in merged:
            merged[group]["count"] += 1
            merged[group]["path"] = group[0]
            merged[group]["detail"] = ""
        else:
            merged[group] = f | {"count": 1}
    return list(merged.values())


def _withhold(raw: dict[str, list[dict]], pending: dict[str, Behavior]) -> None:
    """Drop every rule that would reach beyond the evidence (D15): one that also
    matches a stable value somewhere in the capture, or one for a behavior
    captured with scrub=False (no rule applies to it)."""
    for name, found in raw.items():
        if not pending[name].scrub:
            for f in found:
                if f["rule"]:
                    f.update(rule=None, _match=None)
                    f["note"] += ("; captured with scrub=False, so no scrub rule applies - "
                                  "mask it at capture time if it is noise")
    hits: dict[tuple, Counter] = {}
    for name, found in raw.items():
        for f in found:
            if f["rule"]:
                hits.setdefault(f["_match"], Counter())[name] += 1
    texts: dict[str, str] = {}
    for match, by_name in hits.items():
        kind, what = match
        if kind == "field":
            def count(name: str, key: str = what) -> int:
                return sum(k == key for k in _keys(pending[name].payload))
        else:
            pat = re.compile(what)

            def count(name: str, pat: re.Pattern = pat) -> int:
                if name not in texts:
                    texts[name] = canonical_json(pending[name].payload)
                return len(pat.findall(texts[name]))
        stable = sorted(name for name in pending if count(name) > by_name[name])
        if not stable:
            continue
        shown = ", ".join(stable[:3]) + (" ..." if len(stable) > 3 else "")
        what_hits = f'"{what}"' if kind == "field" else "the pattern"
        for found in raw.values():
            for f in found:
                if f["_match"] == match:
                    f.update(rule=None, _match=None)
                    f["note"] += (f"; no rule: {what_hits} also matches stable values in "
                                  f"{shown} - mask it at capture time in this test instead")


def _conftest_for(names: list[str], pending: dict[str, Behavior]) -> str:
    """The conftest.py whose rules cover exactly these behaviors' tests: rules
    registered there apply only under its directory (D20)."""
    dirs = []
    for name in names:
        source = pending[name].source
        if not source:
            return "conftest.py"   # unknown test: only the root conftest covers it
        dirs.append(posixpath.dirname(source.split("::", 1)[0]))
    common = posixpath.commonpath(dirs) if dirs else ""
    return posixpath.join(common, "conftest.py") if common else "conftest.py"


def diagnose(baseline: dict[str, Behavior], pending: dict[str, Behavior],
             only: set[str] | None = None) -> dict:
    """Per-behavior classified drift + scrub suggestions backed by evidence.

    only: diagnose just these names. The collision check still sees every
    pending behavior, so scoping never widens a suggestion. A suggestion with
    conditional=True applies only if the value is not part of the contract.
    """
    # Fingerprints only: compare() would also build a text diff per change,
    # which doctor never shows and which is slow on big payloads.
    changed = [name for name in sorted(set(baseline) & set(pending))
               if (only is None or name in only)
               and baseline[name].fingerprint() != pending[name].fingerprint()]
    raw = {name: findings(baseline[name].payload, pending[name].payload)
           for name in changed}
    for found in raw.values():
        _constant_shift(found)
    _withhold(raw, pending)
    suggestions: dict[str, dict] = {}
    for name, found in raw.items():
        for f in found:
            if f["rule"]:
                s = suggestions.setdefault(
                    f["rule"], {"rule": f["rule"], "reason": f["note"],
                                "conditional": f["conditional"], "behaviors": []})
                if name not in s["behaviors"]:
                    s["behaviors"].append(name)
    for s in suggestions.values():
        s["conftest"] = _conftest_for(s["behaviors"], pending)
    return {"changed": changed,
            "behaviors": {name: _collapse(found) for name, found in raw.items()},
            "suggestions": list(suggestions.values())}
