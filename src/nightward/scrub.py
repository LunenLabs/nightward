"""Normalization of volatile fields before a payload is fingerprinted.

If timestamps / uuids leak into snapshots, every run looks "changed" and the
tool dies of false positives. Two mechanisms, applied in order:

1. field-aware (`register_field`): masks the *value* of a named dict key at any
   depth. Preferred — replacement is a JSON value, so it can't corrupt the
   payload, and look-alike literals in other fields are left alone.
2. text regex (`register` + built-in timestamp/uuid patterns): scrubs the
   canonical-json text, then re-parses. Fallback for values without a stable
   field name. Tradeoff: a literal string that *looks* like a timestamp also
   gets scrubbed - so a business datetime (deadline, as-of date) is masked too.

Opt out per behavior with `behavior(..., scrub=False)` (no scrubbing at all) or
globally with `disable_defaults()` in conftest.py (built-ins off, custom rules
kept). The plugin counts default masks and `nightward run` reports them, so the
masking is never silent.
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
_defaults_enabled = True


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


def disable_defaults() -> None:
    """Turn off the built-in timestamp/uuid scrubbers for every behavior.

    Call it in conftest.py when datetimes/uuids are your *output* (deadlines,
    event times, deterministic ids). Custom `register`/`register_field` rules
    still apply. For a single behavior use `behavior(..., scrub=False)`.
    """
    global _defaults_enabled
    _defaults_enabled = False


def _reset() -> None:
    """Drop all custom scrubbers and re-enable the defaults (test isolation)."""
    global _defaults_enabled
    _custom.clear()
    _custom_fields.clear()
    _defaults_enabled = True


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
            f"them would silently overwrite each other and hide changes. If these keys "
            f"are real data (e.g. business dates), capture with scrub=False or call "
            f"nightward.scrub.disable_defaults(); if they are volatile, key by "
            f"something stable or use a list of records."
        )
    return out


def scrub(payload: Any) -> Any:
    return scrub_counted(payload)[0]


def scrub_counted(payload: Any, *, enabled: bool = True) -> tuple[Any, int]:
    """Scrub `payload`; also return how many values the built-in scrubbers masked.

    enabled=False skips every scrubber but still validates and normalizes the
    payload through JSON (tuples become lists, keys become strings).
    """
    if not enabled:
        return json.loads(canonical_json(payload)), 0
    if _custom_fields:
        payload = _mask_fields(payload)
    text = canonical_json(payload)
    masked = 0
    if _defaults_enabled:
        for pat, repl in _DEFAULT_SCRUBBERS:
            text, n = pat.subn(repl, text)
            masked += n
    for pat, repl in _custom:
        text = pat.sub(repl, text)
    try:
        return json.loads(text, object_pairs_hook=_unique_keys), masked
    except json.JSONDecodeError as exc:
        raise NightwardError(
            "a scrubber produced invalid JSON. Replacement tokens must stay inside "
            "quoted string values (e.g. '\"<EPOCH>\"', not '<EPOCH>')."
        ) from exc
