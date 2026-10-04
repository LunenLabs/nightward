"""Behavior — one captured, named observation of system output."""
from __future__ import annotations

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
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise NightwardError(f"invalid behavior name {name!r}: reserved device name on Windows")
    for ch in name:
        if ch.isspace() or ch in _FORBIDDEN or ord(ch) < 0x20:
            raise NightwardError(
                f"invalid behavior name {name!r}: no whitespace, control, or path "
                f"characters (/\\<>:\"|?*)"
            )
    return name


def canonical_json(payload: Any) -> str:
    """Stable, human-diffable serialization (sorted keys, pretty-printed).

    Stability matters twice: fingerprints stay consistent across runs, and
    git diffs on the approved files stay meaningful for human review.
    """
    try:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise NightwardError(
            f"behavior payload is not JSON-serializable ({exc}). "
            f"Capture plain dict/list/str/number/bool/None, or convert first."
        ) from exc


@dataclass(frozen=True)
class Behavior:
    name: str                 # golden-set key (slug)
    payload: Any              # normalized observed output
    group: str | None = None  # blast-radius grouping (module / feature)
    semantic: bool = False    # opt-in: judge equivalence by meaning, not fingerprint

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.payload).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = {"name": self.name, "group": self.group, "payload": self.payload}
        if self.semantic:  # omit when False so pre-v0.2 approved files stay byte-stable
            d["semantic"] = True
        return d

    @staticmethod
    def from_dict(d: dict) -> Behavior:
        if not isinstance(d, dict):
            raise NightwardError(f"expected a behavior object, got {type(d).__name__}")
        if not isinstance(d.get("name"), str) or "payload" not in d:
            raise NightwardError("behavior object needs a string 'name' and a 'payload'")
        return Behavior(name=d["name"], payload=d["payload"], group=d.get("group"),
                        semantic=d.get("semantic", False))
