"""Committed project settings: `[tool.nightward]` in pyproject.toml.

The semantic judge is a project decision (D14), not a habit of whoever ran
last: it lives in a committed file, so CLI runs, CI and the MCP agent all
judge the same way, and changing it shows up in a PR diff. `nightward run
--judge` overrides it for one run only and is never inherited.

    [tool.nightward]
    judge = "persona:editor"
    path = "tests/nightward"      # what pytest runs when no path is given

The setting belongs to the project that owns the store, so it is read from the
pyproject.toml nearest the store directory - never from the test path, where a
subpackage's own pyproject could drop or swap the judge (R3-LLM-03, D22).
"""
from __future__ import annotations

import os
from pathlib import Path

from .errors import NightwardError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 (pytest ships tomli there)
    import tomli as tomllib


def find_pyproject(start: str | Path = ".") -> Path | None:
    """The nearest pyproject.toml at or above `start` (a dir, or a file's dir;
    a path that doesn't exist yet, such as a new store, counts as a file)."""
    here = Path(start).resolve()
    if not here.is_dir():
        here = here.parent
    for folder in (here, *here.parents):
        candidate = folder / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _settings(start: str | Path) -> tuple[dict, Path | None]:
    path = find_pyproject(start)
    if path is None:
        return {}, None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise NightwardError(f"can't read {path}: {exc}") from exc
    table = data.get("tool", {}).get("nightward", {})
    if not isinstance(table, dict):
        raise NightwardError(f"{path}: [tool.nightward] must be a table")
    return table, path


def judge_setting(store_dir: str | Path = ".nightward") -> tuple[str | None, Path | None]:
    """The committed judge spec (`[tool.nightward] judge`), validated, and the
    pyproject.toml it came from - the one nearest the store `store_dir`."""
    table, path = _settings(store_dir)
    spec = table.get("judge")
    if spec is None:
        return None, path
    if not isinstance(spec, str) or not spec:
        raise NightwardError(
            f'{path}: [tool.nightward] judge must be a "provider:model" string, '
            f'e.g. judge = "persona:editor"')
    from .judge import parse_spec
    try:
        parse_spec(spec)
    except NightwardError as exc:
        raise NightwardError(f"{path}: [tool.nightward] judge: {exc}") from None
    return spec, path


def capture_path(store_dir: str | Path = ".nightward") -> str | None:
    """The committed capture suite (`[tool.nightward] path`), or None.

    What `run`, `gate --run` and MCP `nightward_run` hand pytest when no path is
    given, so the agent loop runs the captures, not a suite that takes minutes.
    It is relative to its pyproject.toml, not to the cwd: one setting must not
    mean different tests from different directories (D22). Returned relative to
    the cwd where it can be (pytest output stays short), else absolute.
    """
    table, path = _settings(store_dir)
    value = table.get("path")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise NightwardError(f'{path}: [tool.nightward] path must be one test path, '
                             f'e.g. path = "tests/nightward"')
    full = path.parent / value
    try:
        return os.path.relpath(full)
    except ValueError:   # Windows: another drive has no relative path
        return str(full)


def project_judge(store_dir: str | Path = ".nightward") -> str | None:
    """The committed judge spec for the store at `store_dir`, or None."""
    return judge_setting(store_dir)[0]


# ---- review depth ---------------------------------------------------------------
# How much a human reads, and when they are interrupted - never whether they
# decide: no depth approves anything (docs/review-depth.md).
DEPTHS = ("brief", "light", "standard", "detailed", "exhaustive")
DEFAULT_DEPTH = "standard"
# When the agent hands a change to a human (status --json / MCP `ask_human`).
ASK_HUMAN = {"exhaustive": "now", "detailed": "now", "standard": "now",
             "light": "end_of_task", "brief": "at_pr"}
_NEW_OR_REMOVED_FLOOR = "standard"   # a first capture or a shrinking baseline


class ReviewDepths:
    """`[tool.nightward.review]`: a default depth and per-group depths."""

    def __init__(self, default: str = DEFAULT_DEPTH, groups: dict[str, str] | None = None):
        self.default = default
        self.groups = groups or {}

    def of(self, group: str | None, kind: str | None = None, *,
           at_least: str | None = None) -> str:
        """The depth a change of `kind` in `group` is reviewed at. NEW and
        REMOVED never go below standard; `at_least` raises (never lowers) it."""
        depth = self.groups.get(group or "(ungrouped)", self.default)
        floors = [depth]
        if kind in ("NEW", "REMOVED"):
            floors.append(_NEW_OR_REMOVED_FLOOR)
        if at_least:
            floors.append(at_least)
        return max(floors, key=DEPTHS.index)


def _depth(value, where: str) -> str:
    if value not in DEPTHS:
        raise NightwardError(f"{where} must be one of {', '.join(DEPTHS)} "
                             f"(got {value!r})")
    return value


def review_depths(store_dir: str | Path = ".nightward") -> ReviewDepths:
    """The committed review depths for the store at `store_dir` (D22: the
    pyproject.toml nearest the store, like the judge)."""
    table, path = _settings(store_dir)
    review = table.get("review")
    if review is None:
        return ReviewDepths()
    if not isinstance(review, dict):
        raise NightwardError(f"{path}: [tool.nightward.review] must be a table")
    default = _depth(review.get("default", DEFAULT_DEPTH),
                     f"{path}: [tool.nightward.review] default")
    groups = review.get("groups", {})
    if not isinstance(groups, dict):
        raise NightwardError(f"{path}: [tool.nightward.review.groups] must be a table of "
                             f'group = "depth"')
    return ReviewDepths(default, {
        g: _depth(d, f"{path}: [tool.nightward.review.groups] {g!r}")
        for g, d in groups.items()})
