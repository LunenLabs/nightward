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

Failure policy is conservative: if a backend can't judge (no SDK, no key, bad
response), `equivalent` returns None and the caller keeps the fingerprint
verdict (CHANGED). The gate closes loudly rather than opening silently.
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
# Stand-ins that make the judge path fully testable without any API key. Each
# persona is a fixed judging temperament, not a heuristic to trust in prod.

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _normalize(text: str) -> str:
    return " ".join(_WORD_RE.split(text.casefold())).strip()


def _persona_lenient(old: str, new: str) -> tuple[str, str]:
    return SAME, "persona:lenient treats every rewording as equivalent"


def _persona_strict(old: str, new: str) -> tuple[str, str]:
    return DIFFERENT, "persona:strict treats any byte difference as a change"


def _persona_editor(old: str, new: str) -> tuple[str, str]:
    if _normalize(old) == _normalize(new):
        return SAME, "same words modulo case/punctuation/whitespace"
    return DIFFERENT, "wording differs beyond case/punctuation/whitespace"


_PERSONAS = {
    "lenient": _persona_lenient,
    "strict": _persona_strict,
    "editor": _persona_editor,
}


def _persona_backend(model: str, old: str, new: str) -> tuple[str, str]:
    try:
        return _PERSONAS[model](old, new)
    except KeyError:
        raise NightwardError(
            f"unknown judge persona {model!r}; available: {', '.join(sorted(_PERSONAS))}"
        ) from None


# ---- anthropic backend ------------------------------------------------------


def _anthropic_backend(model: str, old: str, new: str) -> tuple[str, str]:  # pragma: no cover
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
    msg = client.messages.create(
        model=model,
        max_tokens=200,
        temperature=0,
        messages=[{"role": "user",
                   "content": _PROMPT.replace("{old}", old).replace("{new}", new)}],
    )
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
    return provider, model


def _as_text(payload: Any) -> str:
    return payload if isinstance(payload, str) else canonical_json(payload)


class Judge:
    """One configured provider:model + a persistent verdict cache."""

    def __init__(self, spec: str, cache_path: Path | None = None):
        self.provider, self.model = parse_spec(spec)
        self.spec = spec
        self.cache_path = Path(cache_path) if cache_path else None
        self._cache: dict[str, dict] = self._load_cache()

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
        if isinstance(hit, dict) and hit.get("verdict") in (SAME, DIFFERENT):
            return Verdict(hit["verdict"], str(hit.get("reason", "")), self.spec, cached=True)
        try:
            verdict, reason = _BACKENDS[self.provider](
                self.model, _as_text(old_payload), _as_text(new_payload)
            )
        except JudgeUnavailable:
            return None
        self._cache[key] = {"verdict": verdict, "reason": reason,
                            "behavior": name, "model": self.spec}
        self._save_cache()
        return Verdict(verdict, reason, self.spec)
