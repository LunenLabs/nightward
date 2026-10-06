"""Behavior — one captured, named observation of system output."""
from __future__ import annotations

import datetime
import decimal
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..errors import NightwardError

# Names double as filenames. Allow Unicode (Hangul etc.) but forbid anything that
# breaks paths or shell ergonomics: path separators, Windows-reserved chars,
# control chars, and whitespace.
_FORBIDDEN = set('/\\<>:"|?*')
# Windows device names: "<name>.approved.json" with one of these stems can't be
# created there, so a store committed elsewhere would break on Windows clones.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL",
                     *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise NightwardError("behavior name must be a non-empty string")
    if len(name) > 200:
        raise NightwardError(f"behavior name too long ({len(name)} chars, max 200)")
    if name in (".", ".."):
        raise NightwardError(f"invalid behavior name {name!r}: reserved name")
    if name.endswith("."):
        raise NightwardError(f"invalid behavior name {name!r}: must not end with '.'")
    if not name.isascii():
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise NightwardError(
                f"invalid behavior name {name!r}: contains a lone surrogate"
            ) from None
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise NightwardError(f"invalid behavior name {name!r}: reserved device name on Windows")
    for ch in name:
        if ch.isspace() or ch in _FORBIDDEN or ord(ch) < 0x20:
            raise NightwardError(
                f"invalid behavior name {name!r}: no whitespace, control, or path "
                f"characters (/\\<>:\"|?*)"
            )
    return name


def _type_name(value: Any) -> str:
    t = type(value)
    return t.__qualname__ if t.__module__ == "builtins" else f"{t.__module__}.{t.__qualname__}"


def _conversion_hint(value: Any) -> str:
    """How to turn a common non-JSON value (numpy, pandas, stdlib) into JSON."""
    t = type(value)
    lib, name = t.__module__.split(".")[0], t.__name__
    if isinstance(value, float):
        return "replace NaN/Infinity with None (or a marker string such as \"NaN\")"
    if lib == "numpy":
        return ("use .tolist()" if name == "ndarray"
                else "use .item() (or int()/float()/bool())")
    if lib == "pandas":
        return {"DataFrame": 'use df.to_dict("records")',
                "Series": "use .tolist() (or .to_dict())",
                "Timestamp": "use .isoformat()"}.get(
                    name, "convert it to plain Python first (e.g. .tolist(), str())")
    if isinstance(value, (datetime.date, datetime.time)):
        return "use .isoformat()"
    if isinstance(value, decimal.Decimal):
        return "use str(x) to keep the exact digits (float(x) may round)"
    if isinstance(value, (set, frozenset)):
        return "use sorted(x)"
    if isinstance(value, (bytes, bytearray)):
        return "use .decode() for text or .hex() for binary"
    return "convert it to dict/list/str/number/bool/None"


def _path(path: str, key: Any) -> str:
    if isinstance(key, str) and key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _find_unjsonable(value: Any, path: str, seen: set[int]) -> str | None:
    """Describe the first value json.dumps rejects (path, type, fix), or None.

    Only runs after a failed dump, so it costs nothing on the happy path.
    """
    if value is None or isinstance(value, (str, int)):   # bool is an int
        return None
    if isinstance(value, float):
        if value == value and value not in (float("inf"), float("-inf")):
            return None
        return f"value at {path} is {value!r}, which is not JSON - {_conversion_hint(value)}."
    if isinstance(value, (list, tuple, dict)):
        if id(value) in seen:
            return f"value at {path} contains itself (circular reference)."
        seen = seen | {id(value)}
        if isinstance(value, dict):
            for k in value:
                if not (k is None or isinstance(k, (str, int, float))):
                    return (f"dict at {path} has a key of type {_type_name(k)} - keys must "
                            f"be str (int/float/bool/None keys become strings).")
            try:
                sorted(value)
            except TypeError:
                kinds = ", ".join(sorted({_type_name(k) for k in value}))
                return (f"dict at {path} mixes key types ({kinds}), which have no stable "
                        f"order - convert the keys to str.")
            items = ((_path(path, k), v) for k, v in value.items())
        else:
            items = ((f"{path}[{i}]", v) for i, v in enumerate(value))
        for sub, v in items:
            found = _find_unjsonable(v, sub, seen)
            if found:
                return found
        return None
    return (f"value at {path} is {_type_name(value)}, which is not JSON - "
            f"{_conversion_hint(value)}.")


def canonical_json(payload: Any) -> str:
    """Stable, human-diffable serialization (sorted keys, pretty-printed).

    Stability matters twice: fingerprints stay consistent across runs, and
    git diffs on the approved files stay meaningful for human review.
    """
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        where = _find_unjsonable(payload, "$", set()) or f"{exc}."
        raise NightwardError(
            f"payload is not JSON-serializable: {where} "
            f"Capture plain dict/list/str/number/bool/None."
        ) from exc
    # A lone surrogate (e.g. JS code-unit slicing, "\ud83d") serializes fine
    # but can't be written as UTF-8 - it would crash the store write long after
    # capture. Reject it here, where the offending test can still fail.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise NightwardError(
            f"behavior payload contains text that is not valid Unicode (a lone "
            f"surrogate {text[exc.start:exc.end]!r}, usually from slicing UTF-16 "
            f"code units). Repair the string first, e.g. "
            f"s.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')."
        ) from exc
    return text


@dataclass(frozen=True)
class Behavior:
    name: str                 # golden-set key (slug)
    payload: Any              # normalized observed output
    group: str | None = None  # blast-radius grouping (module / feature)
    semantic: bool = False    # opt-in: judge equivalence by meaning, not fingerprint
    # pytest nodeid of the capturing test: removal evidence only (see
    # cli.approve). Never part of the fingerprint or the comparison.
    source: str | None = None
    # False when captured with behavior(..., scrub=False): no scrub rule applies,
    # so doctor must never suggest one for it. Not part of the fingerprint.
    scrub: bool = True

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.payload).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = {"name": self.name, "group": self.group, "payload": self.payload}
        if self.semantic:  # omit when False so pre-v0.2 approved files stay byte-stable
            d["semantic"] = True
        if self.source is not None:
            d["source"] = self.source
        if not self.scrub:  # omit the default for the same reason
            d["scrub"] = False
        return d

    @staticmethod
    def from_dict(d: dict) -> Behavior:
        if not isinstance(d, dict):
            raise NightwardError(f"expected a behavior object, got {type(d).__name__}")
        if not isinstance(d.get("name"), str) or "payload" not in d:
            raise NightwardError("behavior object needs a string 'name' and a 'payload'")
        source = d.get("source")
        return Behavior(name=d["name"], payload=d["payload"], group=d.get("group"),
                        semantic=d.get("semantic", False),
                        source=source if isinstance(source, str) else None,
                        scrub=d.get("scrub", True) is not False)
