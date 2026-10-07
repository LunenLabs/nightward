"""Committed project settings: `[tool.nightward]` in pyproject.toml.

The semantic judge is a project decision (D14), not a habit of whoever ran
last: it lives in a committed file, so CLI runs, CI and the MCP agent all
judge the same way, and changing it shows up in a PR diff. `nightward run
--judge` overrides it for one run only and is never inherited.

    [tool.nightward]
    judge = "persona:editor"

The setting belongs to the project that owns the store, so it is read from the
pyproject.toml nearest the store directory - never from the test path, where a
subpackage's own pyproject could drop or swap the judge (R3-LLM-03, D22).
"""
from __future__ import annotations

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


def project_judge(store_dir: str | Path = ".nightward") -> str | None:
    """The committed judge spec for the store at `store_dir`, or None."""
    return judge_setting(store_dir)[0]
