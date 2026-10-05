"""LLM-as-judge: semantic equivalence for nondeterministic *text* behaviors.

Design (docs/superpowers/specs/2026-06-10-nightward-llm-judge-semantic-diff-design.md):
the judge decides **equivalence only** — it can collapse a fingerprint mismatch
into "not a change", but it never approves; baseline changes stay human-CLI-only.

Backend spec is "provider:model", so different LLMs are swappable per run:

    anthropic:claude-haiku-4-5     real API (optional extra: pip install nightward[judge])
    persona:editor                 deterministic, key-free stand-ins (tests / dev / CI)

Verdicts are recorded per (old_fp, new_fp, spec) in the store's
``judge_verdicts.json`` — a **committed** ledger, not a transient cache. That
makes a judged-SAME boundary deterministic on a fresh clone or CI runner (no
re-judging, no key needed to *replay* a ruling), bounds token spend to one call
per new fingerprint pair, and puts every ruling in the PR diff where a human
can review it, exactly like a baseline change.

Failure policy is conservative: if a backend can't judge (no SDK, no key, API
error, bad response), `equivalent` returns None and the caller keeps the
fingerprint verdict (CHANGED). The reason is kept (`Judge.summary`) and lands
in the report, the run output and `status --json`: the gate closes loudly
rather than opening silently.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core.behavior import canonical_json
from .core.diff import SAME
from .errors import NightwardError

DIFFERENT = "DIFFERENT"

_PROMPT = (
    "You are a strict equivalence judge for a regression gate. Two text outputs "
    "of the same system follow. Answer SAME only if they are rephrasings with "
    "identical factual content, numbers, and conclusions. If anything factual "
    "differs, or you are unsure, answer DIFFERENT.\n"
    'Reply with JSON only: {"verdict": "SAME"|"DIFFERENT", "reason": "<short>"}\n'
    "--- OUTPUT A ---\n{old}\n--- OUTPUT B ---\n{new}"
)


@dataclass(frozen=True)
class Verdict:
    verdict: str          # SAME | DIFFERENT
    reason: str
    model: str            # full spec, e.g. "persona:editor"
    cached: bool = False


class JudgeUnavailable(Exception):
    """Backend cannot judge right now (no SDK / key / parseable response)."""


# ---- persona backend: deterministic, key-free judge personas ---------------
# Stand-ins that make the judge path testable without any API key (and give CI
# without a key a conservative option). Both judging personas fail closed (D8):
# a change to any digit or number, sign, currency/unit symbol, operator, emoji,
# negation, key, or value type is DIFFERENT. What each one lets through:
#   editor   collapses case, whitespace, and sentence punctuation (. , ; : !
#            followed by a space or the end). Every word must still match.
#   lenient  also lets ordinary words change ("went up" -> "rose"), so it can
#            pass "approved" -> "denied". Tests and demos only, never real gating.
#   strict   rules every difference DIFFERENT.
# Bump _PERSONA_RULES when these rules change: ledger rulings recorded under
# older rules are re-judged instead of replayed.

_PERSONA_RULES = 2

# One token per match: a number keeps its separators ("120.00" != "120,00"), a
# word is letters only, sentence punctuation counts only before a space or the
# end ("." in "a.b" and "!" in "!=" stay significant), and every other non-space
# character (sign, currency, %, operator, quote, emoji) is a token of its own.
_TOKEN_RE = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)*)|(?P<word>[^\W\d_]+)|(?P<punct>[.,;:!](?=\s|$))|(?P<sym>\S)"
)
_NEGATIONS = frozenset({"not", "no", "never", "none", "nobody", "nothing", "neither",
                        "nor", "nowhere", "cannot", "without"})


def _tokens(text: str) -> list[tuple[str, str]]:
    """(kind, text) tokens with sentence punctuation dropped. A word right after
    a number is a unit ("5 mW", "120 USD")."""
    out: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "punct":
            continue
        if kind == "word" and out and out[-1][0] == "num":
            kind = "unit"
        out.append((kind, m.group()))
    return out


def _editor_key(text: str) -> list[tuple[str, str]]:
    # Case-insensitive words; units keep their case ("5 mW" != "5 MW").
    return [(k, t.casefold() if k == "word" else t) for k, t in _tokens(text)]


def _lenient_key(text: str) -> list[tuple[str, str]]:
    # Ordinary words may change; numbers, symbols, units, negations, and a code
    # naming the next number ("USD 120") may not.
    toks = _tokens(text)
    return [(k, t.casefold() if k == "word" else t) for i, (k, t) in enumerate(toks)
            if k != "word" or t.casefold() in _NEGATIONS
            or (t.isupper() and len(t) > 1 and i + 1 < len(toks) and toks[i + 1][0] == "num")]


def _same_by(old: Any, new: Any, key) -> bool:
    """Strings compare by `key`; everything else (keys, structure, value types,
    numbers, booleans) must match exactly."""
    if isinstance(old, str) and isinstance(new, str):
        return key(old) == key(new)
    if type(old) is not type(new):
        return False
    if isinstance(old, dict):
        return old.keys() == new.keys() and all(_same_by(old[k], new[k], key) for k in old)
    if isinstance(old, list):
        return len(old) == len(new) and all(
            _same_by(o, n, key) for o, n in zip(old, new, strict=True))
    return old == new


def _persona_lenient(old: Any, new: Any) -> tuple[str, str]:
    if _same_by(old, new, _lenient_key):
        return SAME, "only wording differs; numbers, symbols, negations and types match"
    return DIFFERENT, "numbers, symbols, units, negations, keys or value types differ"


def _persona_strict(old: Any, new: Any) -> tuple[str, str]:
    return DIFFERENT, "persona:strict treats any byte difference as a change"


def _persona_editor(old: Any, new: Any) -> tuple[str, str]:
    if _same_by(old, new, _editor_key):
        return SAME, "only case, whitespace or sentence punctuation differ"
    return DIFFERENT, "content differs beyond case/whitespace/sentence punctuation"


_PERSONAS = {
    "lenient": _persona_lenient,
    "strict": _persona_strict,
    "editor": _persona_editor,
}


def _persona_backend(model: str, old: Any, new: Any) -> tuple[str, str]:
    try:
        persona = _PERSONAS[model]
    except KeyError:
        raise NightwardError(
            f"unknown judge persona {model!r}; available: {', '.join(sorted(_PERSONAS))}"
        ) from None
    return persona(old, new)


# ---- anthropic backend ------------------------------------------------------


def _anthropic_backend(model: str, old: Any, new: Any) -> tuple[str, str]:  # pragma: no cover
    # Needs network + ANTHROPIC_API_KEY; exercised manually, not in CI.
    try:
        import anthropic
    except ImportError as exc:
        raise JudgeUnavailable(
            "anthropic SDK not installed - pip install 'nightward[judge]'"
        ) from exc
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise JudgeUnavailable("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user",
                       "content": _PROMPT.replace("{old}", _as_text(old))
                                         .replace("{new}", _as_text(new))}],
        )
    except anthropic.APIError as exc:  # auth, network, rate limit, unknown model
        raise JudgeUnavailable(f"API error: {exc}") from exc
    try:
        data = json.loads(msg.content[0].text)
        verdict = data["verdict"]
        if verdict not in (SAME, DIFFERENT):
            raise ValueError(f"bad verdict {verdict!r}")
        return verdict, str(data.get("reason", ""))
    except (ValueError, KeyError, IndexError, AttributeError) as exc:
        raise JudgeUnavailable(f"unparseable judge response: {exc}") from exc


_BACKENDS = {
    "persona": _persona_backend,
    "anthropic": _anthropic_backend,
}


def parse_spec(spec: str) -> tuple[str, str]:
    provider, sep, model = spec.partition(":")
    if not sep or not provider or not model:
        raise NightwardError(
            f"invalid judge spec {spec!r}: expected 'provider:model', "
            f"e.g. 'anthropic:claude-haiku-4-5' or 'persona:editor'"
        )
    if provider not in _BACKENDS:
        raise NightwardError(
            f"unknown judge provider {provider!r}; available: {', '.join(sorted(_BACKENDS))}"
        )
    # Persona names are known up front: reject a typo before a whole suite runs.
    if provider == "persona" and model not in _PERSONAS:
        raise NightwardError(
            f"unknown judge persona {model!r}; available: {', '.join(sorted(_PERSONAS))}"
        )
    return provider, model


def _as_text(payload: Any) -> str:
    return payload if isinstance(payload, str) else canonical_json(payload)


_EXCERPT = 1000  # chars of each side kept in the ledger


def _excerpt(payload: Any) -> str:
    text = _as_text(payload)
    return text if len(text) <= _EXCERPT else text[:_EXCERPT] + " ...[truncated]"


class Judge:
    """One configured provider:model + a persistent verdict cache."""

    def __init__(self, spec: str, cache_path: Path | None = None):
        self.provider, self.model = parse_spec(spec)
        self.spec = spec
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict] = self._load_cache()
        # Why the backend could not rule this run, and which behaviors fell back
        # to the exact comparison because of it - surfaced, never swallowed.
        self.unavailable: str | None = None
        self.compared_exactly: list[str] = []

    def _load_cache(self) -> dict[str, dict]:
        # The ledger is committed, so it can be corrupted by e.g. a merge
        # conflict. Fail loudly: silently starting empty would overwrite the
        # recorded rulings on the next save.
        if not (self.cache_path and self.cache_path.exists()):
            return {}
        try:
            ledger = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NightwardError(
                f"corrupt judge verdict ledger {self.cache_path}: {exc} "
                f"(resolve it by hand, or delete it to re-judge from scratch)"
            ) from exc
        if not isinstance(ledger, dict):
            raise NightwardError(
                f"corrupt judge verdict ledger {self.cache_path}: expected a JSON object"
            )
        return ledger

    def _save_cache(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )

    def equivalent(self, old_payload: Any, new_payload: Any,
                   old_fp: str, new_fp: str, name: str = "") -> Verdict | None:
        """Judge a fingerprint mismatch. None = unavailable -> keep CHANGED.

        Each new ruling is appended to the verdict ledger and saved. The ledger
        is meant to be COMMITTED (it is the durable record that keeps a
        judged-SAME boundary intact on a fresh clone / CI runner) and reviewed
        in PRs like any baseline change — `name` is recorded so the diff is
        readable by a human.
        """
        key = f"{old_fp}:{new_fp}:{self.spec}"
        hit = self._cache.get(key)
        if (self.provider == "persona" and isinstance(hit, dict)
                and hit.get("rules") != _PERSONA_RULES):
            hit = None  # ruled under older persona rules: re-judge (free, deterministic)
        if isinstance(hit, dict) and hit.get("verdict") in (SAME, DIFFERENT):
            return Verdict(hit["verdict"], str(hit.get("reason", "")), self.spec, cached=True)
        try:
            verdict, reason = _BACKENDS[self.provider](self.model, old_payload, new_payload)
        except JudgeUnavailable as exc:
            self.unavailable = self.unavailable or str(exc)
            self.compared_exactly.append(name)
            return None
        # The wording ruled on is kept too: pending/ is not committed, so a PR
        # reviewer would otherwise see only two hashes (R1-LLM-04).
        self._cache[key] = {"verdict": verdict, "reason": reason,
                            "behavior": name, "model": self.spec,
                            "old": _excerpt(old_payload), "new": _excerpt(new_payload)}
        if self.provider == "persona":
            self._cache[key]["rules"] = _PERSONA_RULES
        self._save_cache()
        return Verdict(verdict, reason, self.spec)

    def summary(self) -> dict:
        """What the report records about this judge (see runner.recompute)."""
        return {"spec": self.spec, "unavailable": self.unavailable,
                "compared_exactly": sorted(self.compared_exactly)}
