"""Normalization of volatile fields before a payload is fingerprinted.

If timestamps / uuids leak into snapshots, every run looks "changed" and the
tool dies of false positives. Two mechanisms, applied in order:

1. field-aware (`register_field`): masks the *value* of a named dict key at any
   depth. Preferred — replacement is a JSON value, so it can't corrupt the
   payload, and look-alike literals in other fields are left alone.
2. text regex (`register` + built-in timestamp/uuid patterns): scrubs the
   canonical-json text, then re-parses. Fallback for values without a stable
   field name. Tradeoff: a literal string that *looks* like a timestamp also
   gets scrubbed.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .core.behavior import canonical_json
from .errors import NightwardError

_DEFAULT_SCRUBBERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"), "<TIMESTAMP>"),  # noqa: E501
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),  # noqa: E501
]

_custom: list[tuple[re.Pattern, str]] = []
_custom_fields: dict[str, Any] = {}


def register(pattern: str, replacement: str) -> None:
    """Register a project-specific scrubber, e.g. register(r'"ord_\\d+"', '"<ORDER_ID>"').

    Replacements must keep the payload valid JSON: only substitute text *inside*
    quoted string values, and quote your placeholder tokens. Prefer
    `register_field` when the volatile value lives under a stable key.
    """
    _custom.append((re.compile(pattern), replacement))


def register_field(field: str, replacement: Any = "<SCRUBBED>") -> None:
    """Mask the value of every dict key named `field`, at any depth.

    e.g. register_field("created_at") or register_field("attempts", 0).
    The replacement is a JSON value, not regex text — it cannot corrupt the
    payload and never touches look-alike literals in other fields.
    """
    _custom_fields[field] = replacement


def _reset() -> None:
    """Drop all custom scrubbers (test isolation)."""
    _custom.clear()
    _custom_fields.clear()


def _mask_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _custom_fields[k] if k in _custom_fields else _mask_fields(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_fields(v) for v in value]
    return value


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    out = dict(pairs)
    if len(out) != len(pairs):
        dupes = sorted(k for k, n in Counter(k for k, _ in pairs).items() if n > 1)
        raise NightwardError(
            f"scrubbing collapsed distinct dict keys into {dupes}: the values behind "
            f"them would silently overwrite each other and hide changes. Don't key "
            f"dicts by volatile values (timestamps/uuids) - use a list of records."
        )
    return out


def scrub(payload: Any) -> Any:
    if _custom_fields:
        payload = _mask_fields(payload)
    text = canonical_json(payload)
    for pat, repl in (*_DEFAULT_SCRUBBERS, *_custom):
        text = pat.sub(repl, text)
    try:
        return json.loads(text, object_pairs_hook=_unique_keys)
    except json.JSONDecodeError as exc:
        raise NightwardError(
            "a scrubber produced invalid JSON. Replacement tokens must stay inside "
            "quoted string values (e.g. '\"<EPOCH>\"', not '<EPOCH>')."
        ) from exc
