"""Capture coverage: which public functions ran while a capture test ran.

Information, never a verdict (spec 2026-10-15, option A): a green gate over three
captures must not look like one over three hundred. "Ran in a capture test" is
not "protected" - a function can run without its result reaching any payload.

Measured with sys.setprofile, so there is no dependency and coverage.py / pytest-cov
(which use the tracer) keep working alongside. Only `call` events are recorded,
as code objects; they are matched to source definitions at the end of the run.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import sys
import threading
from pathlib import Path

# Uncovered names kept in run_meta / the report; the counts stay exact.
MAX_LISTED = 200
_SKIP_DIRS = ("__pycache__", "node_modules", "build", "dist", "venv", "*.egg", ".*")


class Tracer:
    """Records the code objects called between start() and stop()."""

    def __init__(self) -> None:
        self.codes: set = set()
        self._previous = None

    def _profile(self, frame, event, arg):
        if event == "call":
            self.codes.add(frame.f_code)

    def start(self) -> None:
        self._previous = sys.getprofile()
        sys.setprofile(self._profile)
        threading.setprofile(self._profile)

    def stop(self) -> None:
        sys.setprofile(self._previous)
        threading.setprofile(None)

    def called(self) -> set[tuple[str, str, int]]:
        files = {c.co_filename for c in self.codes}
        real = {f: _key(f) for f in files}
        return {(real[c.co_filename], c.co_name, c.co_firstlineno) for c in self.codes}


def _key(path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _first_line(node) -> int:
    # co_firstlineno of a decorated function is its first decorator's line
    return min([d.lineno for d in node.decorator_list] + [node.lineno])


def _defs(tree: ast.Module):
    """(qualified name, name, first line) of the public functions of a module."""
    func = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, func) and not node.name.startswith("_"):
            yield node.name, node.name, _first_line(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for item in node.body:
                if isinstance(item, func) and not item.name.startswith("_"):
                    yield f"{node.name}.{item.name}", item.name, _first_line(item)


def _sources(root: Path, test_patterns: list[str]):
    if root.is_file():
        yield root
        return
    for here, dirs, files in os.walk(root):
        base = Path(here)
        dirs[:] = sorted(d for d in dirs
                         if not any(fnmatch.fnmatch(d, p) for p in _SKIP_DIRS)
                         and not (base / d / "pyvenv.cfg").exists())
        for f in sorted(files):
            if (f.endswith(".py") and f != "conftest.py"
                    and not any(fnmatch.fnmatch(f, p) for p in test_patterns)):
                yield base / f


def _shown(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def summarize(base: str, roots: list[str], called: set[tuple[str, str, int]],
              test_patterns: list[str]) -> dict:
    """{roots, functions, executed, uncovered (sorted, at most MAX_LISTED),
    unreadable (files that don't parse)} for the public functions under roots."""
    base_path = Path(base)
    functions = executed = 0
    uncovered, unreadable = [], []
    for root in roots:
        for path in _sources(Path(root), test_patterns):
            shown = _shown(path, base_path)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError, OSError):
                unreadable.append(shown)
                continue
            key = _key(path)
            for qualname, name, line in _defs(tree):
                functions += 1
                if (key, name, line) in called:
                    executed += 1
                else:
                    uncovered.append(f"{shown}:{qualname}")
    uncovered.sort()
    return {"roots": [_shown(Path(r), base_path) for r in roots],
            "functions": functions, "executed": executed,
            "uncovered": uncovered[:MAX_LISTED], "unreadable": unreadable}
