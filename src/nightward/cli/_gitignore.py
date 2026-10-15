"""What git must commit and must ignore in the store (init, run, reject, view)."""
from __future__ import annotations

from pathlib import Path

from rich.markup import escape

from ._common import (
    DEFAULT_SITE,
    GITIGNORE_HEADER,
    LEGACY_ENTRIES,
    TRANSIENT_ENTRIES,
    _git,
    err_console,
)


def _store_prefix(dir_: str) -> str | None:
    """The store as a path a .gitignore here can name, or None when it lives
    outside the current directory."""
    p = Path(dir_)
    if p.is_absolute():
        try:
            p = p.relative_to(Path.cwd())
        except ValueError:
            return None
    prefix = p.as_posix()
    if prefix == ".." or prefix.startswith("../"):
        return None
    return prefix


def _gitignore_lines(dir_: str) -> list[str] | None:
    """Ignore rules for the store's transient entries, or None when the store
    lives outside the current directory (a .gitignore here can't name it)."""
    prefix = _store_prefix(dir_)
    if prefix is None:
        return None
    return [GITIGNORE_HEADER, f"{DEFAULT_SITE}/",
            *(f"{prefix}/{entry}" for entry in TRANSIENT_ENTRIES)]


def _legacy_gitignore_lines(dir_: str) -> list[str]:
    prefix = _store_prefix(dir_)
    return [] if prefix is None else [f"{prefix}/{entry}" for entry in LEGACY_ENTRIES]


def _git_ignored(path: Path) -> bool | None:
    """Whether git ignores `path` (None: no git, or not inside a repository)."""
    r = _git("check-ignore", "-q", str(path))
    return None if r is None else {0: True, 1: False}.get(r.returncode)


# What git must commit (the boundary and the decisions on it) and what it must
# not (per-run state), as probe paths under the store (R3-FIN-06, R3-OPS-04).
_COMMITTED_PROBES = (
    ("baseline/nightward-probe.approved.json",
     "the approved baseline ({store}/baseline/) is git-ignored by `{rule}`: it can't be "
     "committed, so CI and teammates see every behavior as NEW - remove that rule "
     "(nightward's own rules ignore only the per-run files)"),
    ("rejected/nightward-probe.rejected.json",
     "rejections ({store}/rejected/) are git-ignored by `{rule}`: they won't reach other "
     "clones - {fix}"),
    ("judge/nightward-probe.json",       # the ledger: one file per ruling (D22)
     "the judge ledger ({store}/judge/) is git-ignored by `{rule}`: fresh clones and CI "
     "can't replay its rulings - remove that rule"),
)
# The single-file ledger older versions wrote: still read, so still committed.
_LEGACY_LEDGER_PROBE = (
    "judge_verdicts.json",
    "the legacy judge verdict ledger ({store}/judge_verdicts.json) is git-ignored by "
    "`{rule}`: fresh clones and CI can't replay its rulings - remove that rule")
_TRANSIENT_PROBES = ("report.json", "run_meta.json", "reviewed.json", ".lock",
                     "pending/nightward-probe.received.json")


def _ignore_rules(paths: list[str]) -> dict[str, str] | None:
    """{path: "source:line:pattern"} for each of `paths` git ignores (None: no git)."""
    # -z: NUL-separated and unquoted - without it git quotes a non-ASCII path
    # ("\354\240\200...") and it would never match the path we passed.
    r = _git("check-ignore", "-v", "-z", "--no-index", "--stdin", input="\0".join(paths) + "\0")
    if r is None or r.returncode not in (0, 1):   # not a repository, or outside it
        return None
    fields = r.stdout.split("\0")
    rules = {}
    for source, line, pattern, path in zip(*[iter(fields)] * 4, strict=False):
        if pattern.startswith("!"):
            continue                   # a negation: matched, but not ignored
        rules[path] = f"{source}:{line}:{pattern}"
    return rules


def _ignore_problems(dir_: str) -> list[str]:
    """What the .gitignore rules get wrong for this store: committed entries that
    are ignored (an old or overbroad rule), or per-run files that are not."""
    root = Path(dir_)
    probes = list(_COMMITTED_PROBES)
    if (root / _LEGACY_LEDGER_PROBE[0]).exists():
        probes.append(_LEGACY_LEDGER_PROBE)
    committed = {(root / p).as_posix(): msg for p, msg in probes}
    transient = {(root / p).as_posix(): p.split("/")[0] + ("/" if "/" in p else "")
                 for p in _TRANSIENT_PROBES}
    rules = _ignore_rules([*committed, *transient])
    if rules is None:
        return []
    problems, named = [], set()
    for p, msg in committed.items():
        rule = rules.get(p)
        if rule is None or rule in named:   # one overbroad rule: say it once
            continue
        named.add(rule)
        # Only the rejected/ line older `init`s wrote is init's to remove.
        fix = ("re-run `nightward init` to update .gitignore"
               if rule.endswith(f"{root.as_posix()}/rejected/") else "remove that rule")
        problems.append(msg.format(store=root.as_posix(), rule=rule, fix=fix))
    loose = [name for p, name in transient.items() if p not in rules]
    if loose:
        problems.append(f"per-run files are not git-ignored ({', '.join(loose)}) and must not "
                        f"be committed - re-run `nightward init` to add the .gitignore rules")
    return problems


def _warn_ignore_problems(dir_: str) -> None:
    for problem in _ignore_problems(dir_):
        err_console.print(f"[yellow]warning:[/yellow] {escape(problem)}", soft_wrap=True)


def _warn_unless_ignored(path: Path, what: str,
                         fix: str = "run `nightward init` to add the .gitignore rules") -> None:
    # Only a nudge: a rule in a parent .gitignore or info/exclude counts too.
    if _git_ignored(path) is False:
        err_console.print(f"[yellow]warning:[/yellow] {escape(str(path))} is not git-ignored "
                          f"and {what} - {escape(fix)}", soft_wrap=True)


def _ignore_fix(out_path: Path) -> str:
    """How to ignore a dashboard dir: init knows only the default one (R1-WEB-06)."""
    rule = _store_prefix(str(out_path))
    if rule == DEFAULT_SITE:
        return "run `nightward init` to add the .gitignore rules"
    return f"add `{rule or out_path.as_posix()}/` to .gitignore"


