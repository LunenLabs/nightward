"""Domain maps: the rules a behavior guards, shown next to its diff.

An agent writes a map before the first capture (the `nightward:onboard` skill):
what each rule is, where the code does it, and where the intent is stated. The
core only reads maps - no LLM here - and `review` prints a behavior's rules
above its diff at the `detailed` and `exhaustive` depths, so a reviewer sees
what a value should be next to what it became.

A map is a Markdown file under `[tool.nightward] domain` (default
`docs/nightward-domain/`, relative to the pyproject.toml) with TOML front matter
between `+++` lines. The body is free prose for humans; only the front matter is
read:

    +++
    group = "pt.payment"
    derived_from = "bd28deb"          # the commit that was analysed

    [rules.R3]
    rule = "A late FAILED webhook never moves a paid payment back"
    code = "app/apps/pettracker/service.py:851"
    intent = "docstring of handle_portone_webhook_event"   # omit: unconfirmed

    [behaviors]
    "pt.portone_webhook" = ["R3"]
    +++

A rule without `intent` was inferred from the code alone: the code's bugs read
as rules that way, so it is shown as unconfirmed.

A map is a display aid: one that can't be read is a warning, never an error
that blocks a review.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import _settings, find_pyproject

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib

DEFAULT_DIR = "docs/nightward-domain"
_FENCE = "+++"


@dataclass(frozen=True)
class Rule:
    id: str
    rule: str
    code: str | None
    intent: str | None
    source: str          # the map file, for the reviewer to open


@dataclass
class DomainMaps:
    rules: dict[str, list[Rule]]      # behavior name -> the rules it guards
    warnings: list[str]

    def of(self, name: str) -> list[Rule]:
        return self.rules.get(name, [])


def maps_dir(store_dir: str | Path) -> Path:
    """Where the maps of the project owning the store live."""
    table, pyproject = _settings(store_dir)
    root = pyproject.parent if pyproject else Path(store_dir).resolve().parent
    value = table.get("domain", DEFAULT_DIR)
    return root / value if isinstance(value, str) and value.strip() else root / DEFAULT_DIR


def _front_matter(text: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FENCE:
            return "\n".join(lines[1:i])
    raise ValueError(f"no closing {_FENCE} line")


def _shown(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def load(store_dir: str | Path) -> DomainMaps:
    """Every map's rules by behavior name. Never raises for a bad map."""
    folder = maps_dir(store_dir)
    pyproject = find_pyproject(store_dir)
    root = pyproject.parent if pyproject else folder.parent
    out = DomainMaps({}, [])
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.md")):
        where = _shown(path, root)
        try:
            front = _front_matter(path.read_text(encoding="utf-8"))
            if front is None:
                continue                       # prose only: nothing for the core
            data = tomllib.loads(front)
        except (OSError, UnicodeDecodeError, ValueError) as exc:   # TOMLDecodeError too
            out.warnings.append(f"{where}: can't read its front matter ({exc})")
            continue
        rules = data.get("rules", {})
        behaviors = data.get("behaviors", {})
        if not isinstance(rules, dict) or not isinstance(behaviors, dict):
            out.warnings.append(f"{where}: [rules] and [behaviors] must be tables")
            continue
        for name, ids in behaviors.items():
            if isinstance(ids, str):
                ids = [ids]
            if not isinstance(ids, list):
                out.warnings.append(f"{where}: behaviors.{name} must list rule ids")
                continue
            for rid in ids:
                r = rules.get(rid)
                if not isinstance(r, dict) or not isinstance(r.get("rule"), str):
                    out.warnings.append(f"{where}: {name} guards {rid!r}, which has no "
                                        f"[rules.{rid}] with a `rule` text")
                    continue
                out.rules.setdefault(name, []).append(Rule(
                    id=str(rid), rule=r["rule"],
                    code=r.get("code") if isinstance(r.get("code"), str) else None,
                    intent=r.get("intent") if isinstance(r.get("intent"), str) else None,
                    source=where))
    return out
