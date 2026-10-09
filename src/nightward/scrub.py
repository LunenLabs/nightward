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

Scope (D20, D28): a rule (or `disable_defaults()`) must be called directly from
a conftest.py or a test module, and applies only to behaviors captured by tests
under that file's directory, like a conftest's own fixtures. So a rule in the
root conftest.py covers the whole suite, while one in `services/orders/` can't
mask a field of `services/billing`, and a capture never depends on which
directories a run collected. A call from any other file (product code, a helper,
a plugin) is refused: a rule there could rewrite a regression back into the
approved value with nothing in the test diff. The plugin counts every custom
rule's replacements per behavior and `nightward run` reports them when they
change.
"""
from __future__ import annotations

import fnmatch
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

# Each rule carries its scope: the conftest.py or test module that registered it.
_custom: list[tuple[re.Pattern, str, Path | None]] = []
_custom_fields: list[tuple[str, Any, Path | None]] = []
_defaults_off: list[Path | None] = []   # where disable_defaults() was called
# pytest's python_files (the plugin sets them from the ini): what a test module is.
_test_files: list[str] = ["test_*.py", "*_test.py"]
# Matches per custom rule in this process, so a rule that never fires is
# reported instead of silently leaving the noise in place (R1-WEB-03).
_hits: Counter = Counter()


def set_test_files(patterns: list[str]) -> None:
    """Which file names are test modules (pytest's python_files)."""
    _test_files[:] = list(patterns) or ["test_*.py", "*_test.py"]


def _caller_scope(what: str) -> Path:
    """The conftest.py or test module that called scrub.<what>() directly.

    Anything else is refused (D28): a rule in product code or a shared helper
    is not test-owned, and could silently rewrite captured values."""
    frame = sys._getframe(2)
    path = Path(frame.f_code.co_filename)
    if path.name == "conftest.py" or any(fnmatch.fnmatch(path.name, p) for p in _test_files):
        path = path.resolve()
        _shown(path)   # name it relative to where it was registered
        return path
    raise NightwardError(
        f"scrub.{what}() was called from {path}:{frame.f_lineno}, which is neither a "
        f"conftest.py nor a test module. Register scrub rules directly in a conftest.py "
        f"(or a test module): they apply to the tests under its directory, and a "
        f"reviewer sees them as test configuration. A rule in product code could "
        f"silently rewrite what the gate captures.")


def _applies(scope: Path | None, path: Path | None) -> bool:
    """Whether a rule registered from `scope` covers the test file `path`
    (None: not captured by a test - library use - every rule applies)."""
    if scope is None or path is None:
        return True
    return Path(path).resolve().is_relative_to(scope.parent)


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
    that matched nothing, and how many values a rule replaced when that count
    changes. Call it directly in a conftest.py or a test module: the rule covers
    only tests under that file's directory; any other caller is refused.
    """
    _custom.append((re.compile(pattern), replacement, _caller_scope("register")))


def register_field(field: str, replacement: Any = "<SCRUBBED>") -> None:
    """Mask the value of every dict key named `field`, at any depth.

    e.g. register_field("created_at") or register_field("attempts", 0).
    The replacement is a JSON value, not regex text — it cannot corrupt the
    payload and never touches look-alike literals in other fields. Call it
    directly in a conftest.py or a test module: the rule covers only tests under
    that file's directory; any other caller is refused.
    """
    _register_field_scoped(field, replacement, _caller_scope("register_field"))


def _register_field_scoped(field: str, replacement: Any, scope: Path | None) -> None:
    _custom_fields.append((field, replacement, scope))


def disable_defaults() -> None:
    """Turn off the built-in timestamp/uuid scrubbers for every behavior.

    Call it in conftest.py when datetimes/uuids are your *output* (deadlines,
    event times, deterministic ids). Custom `register`/`register_field` rules
    still apply. For a single behavior use `behavior(..., scrub=False)`. Call
    it directly in a conftest.py or a test module: it covers only tests under
    that file's directory.
    """
    _defaults_off.append(_caller_scope("disable_defaults"))


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


def _placeholder(replacement: Any) -> bool:
    """A mask writes a visible <TOKEN>; anything else rewrites the value."""
    if not isinstance(replacement, str):
        return False
    text = re.sub(r"\\(?:\d+|g<\w+>)", "", replacement)
    return bool(re.search(r"<[^<>]+>", text))


def rules() -> list[tuple[str, bool]]:
    """(rule text, replacement is a <PLACEHOLDER>) for every custom rule."""
    return [*((_rule_text(p, s), _placeholder(r)) for p, r, s in _custom),
            *((_rule_text(f, s), _placeholder(r)) for f, r, s in _custom_fields)]


def hits() -> Counter:
    """Replacements per custom rule text so far in this process (a copy)."""
    return Counter(_hits)


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
