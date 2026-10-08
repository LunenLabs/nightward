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
with `disable_defaults()` in conftest.py (built-ins off, custom rules kept). The
plugin counts default masks and `nightward run` reports them, so the masking is
never silent.

Scope (D20): a rule (or `disable_defaults()`) called from a conftest.py - also
through a helper it calls - applies only to behaviors captured by tests under
that conftest's directory, like the conftest's own fixtures. So a rule in the
root conftest.py covers the whole suite, while one in `services/orders/conftest.py`
can't mask a field of `services/billing`, and a capture never depends on which
directories a run collected. Rules registered anywhere else are global.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .core.behavior import canonical_json
from .errors import NightwardError

_DEFAULT_SCRUBBERS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"), "<TIMESTAMP>"),  # noqa: E501
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<UUID>"),  # noqa: E501
]

# Each rule carries its scope: the conftest.py that registered it, or None (global).
_custom: list[tuple[re.Pattern, str, Path | None]] = []
_custom_fields: list[tuple[str, Any, Path | None]] = []
_defaults_off: list[Path | None] = []   # where disable_defaults() was called
# Matches per custom rule in this process, so a rule that never fires is
# reported instead of silently leaving the noise in place (R1-WEB-03).
_hits: Counter = Counter()


def _caller_conftest() -> Path | None:
    """The conftest.py on the call stack nearest the caller, or None."""
    frame = sys._getframe(2)
    while frame is not None:
        path = Path(frame.f_code.co_filename)
        if path.name == "conftest.py":
            path = path.resolve()
            _shown(path)   # name it relative to where it was registered
            return path
        frame = frame.f_back
    return None


def _applies(scope: Path | None, path: Path | None) -> bool:
    """Whether a rule registered from `scope` covers the test file `path`."""
    if scope is None:
        return True
    return path is not None and Path(path).resolve().is_relative_to(scope.parent)


def register(pattern: str, replacement: str) -> None:
    """Register a project-specific scrubber, e.g. register(r'"ord_\\d+"', '"<ORDER_ID>"').

    The pattern runs over the canonical JSON *text* of the payload, not over
    decoded values: pretty-printed (`"key": "value"`, keys sorted), and inside a
    string value every `"` is written `\\"` and every newline `\\n`. So the
    HTML `value="abc"` inside a body is matched by r'value=\\"abc', and `^`/`$`
    never see the lines of a multi-line string.
    Replacements must keep the payload valid JSON: only substitute text *inside*
    quoted string values, and quote your placeholder tokens. Prefer
    `register_field` when the volatile value lives under a stable key, or mask
    the value in the test before capturing it. `nightward run` reports a rule
    that matched nothing. Called from a conftest.py, the rule covers only tests
    under that conftest's directory.
    """
    _custom.append((re.compile(pattern), replacement, _caller_conftest()))


def register_field(field: str, replacement: Any = "<SCRUBBED>") -> None:
    """Mask the value of every dict key named `field`, at any depth.

    e.g. register_field("created_at") or register_field("attempts", 0).
    The replacement is a JSON value, not regex text — it cannot corrupt the
    payload and never touches look-alike literals in other fields. Called from
    a conftest.py, the rule covers only tests under that conftest's directory.
    """
    _register_field_scoped(field, replacement, _caller_conftest())


def _register_field_scoped(field: str, replacement: Any, scope: Path | None) -> None:
    _custom_fields.append((field, replacement, scope))


def disable_defaults() -> None:
    """Turn off the built-in timestamp/uuid scrubbers for every behavior.

    Call it in conftest.py when datetimes/uuids are your *output* (deadlines,
    event times, deterministic ids). Custom `register`/`register_field` rules
    still apply. For a single behavior use `behavior(..., scrub=False)`. Called
    from a conftest.py, it covers only tests under that conftest's directory.
    """
    _defaults_off.append(_caller_conftest())


def _reset() -> None:
    """Drop all custom scrubbers and re-enable the defaults (test isolation)."""
    _custom.clear()
    _custom_fields.clear()
    _hits.clear()
    _defaults_off.clear()


def _rule_text(rule: re.Pattern | str, scope: Path | None) -> str:
    text = (f"register_field({rule!r})" if isinstance(rule, str)
            else f"register(r'{rule.pattern}')")
    return text if scope is None else f"{text} in {_shown(scope)}"


_SHOWN: dict[Path, str] = {}


def _shown(path: Path) -> str:
    # Fixed on first use, so a test that changes directory can't split the hit
    # count of one rule across two names.
    if path not in _SHOWN:
        try:
            _SHOWN[path] = path.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            _SHOWN[path] = str(path)
    return _SHOWN[path]


def unmatched_rules() -> list[str]:
    """Custom rules that have matched nothing so far in this process."""
    rules = [*((pat, scope) for pat, _, scope in _custom),
             *((field, scope) for field, _, scope in _custom_fields)]
    return [text for text in (_rule_text(*r) for r in rules) if not _hits[text]]


def _mask_fields(value: Any, fields: dict[str, tuple[Any, str]]) -> Any:
    # fields: key -> (replacement, rule text for the hit count)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in fields:
                _hits[fields[k][1]] += 1
                out[k] = fields[k][0]
            else:
                out[k] = _mask_fields(v, fields)
        return out
    if isinstance(value, list):
        return [_mask_fields(v, fields) for v in value]
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


def scrub(payload: Any, path: Path | None = None) -> Any:
    return scrub_counted(payload, path=path)[0]


def scrub_counted(payload: Any, *, enabled: bool = True,
                  path: Path | None = None) -> tuple[Any, int]:
    """Scrub `payload`; also return how many values the built-in scrubbers masked.

    path is the capturing test's file: rules registered from a conftest.py
    apply only when it lies under that conftest's directory (None: only global
    rules apply). enabled=False skips every scrubber but still validates and
    normalizes the payload through JSON (tuples become lists, keys become strings).
    """
    if not enabled:
        return json.loads(canonical_json(payload)), 0
    fields = {field: (repl, _rule_text(field, scope))
              for field, repl, scope in _custom_fields if _applies(scope, path)}
    if fields:
        payload = _mask_fields(payload, fields)
    text = canonical_json(payload)
    masked = 0
    if not any(_applies(scope, path) for scope in _defaults_off):
        for pat, repl in _DEFAULT_SCRUBBERS:
            text, n = pat.subn(repl, text)
            masked += n
    for pat, repl, scope in _custom:
        if _applies(scope, path):
            text, n = pat.subn(repl, text)
            _hits[_rule_text(pat, scope)] += n
    try:
        return json.loads(text, object_pairs_hook=_unique_keys), masked
    except json.JSONDecodeError as exc:
        raise NightwardError(
            "a scrubber produced invalid JSON. Replacement tokens must stay inside "
            "quoted string values (e.g. '\"<EPOCH>\"', not '<EPOCH>')."
        ) from exc
