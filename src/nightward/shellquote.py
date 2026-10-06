"""Quote behavior names for the shells people paste commands into.

Names come from test code, which an agent may write, and the dashboard's
copy-paste commands land in the approver's own shell. A name such as
`x;touch${IFS}pwned` must arrive as one literal argument, never as code
(R2-WEB-02). One string can't be safe in every shell, so each shell gets its
own form, and a name that has no safe form in a shell gets None.
"""
from __future__ import annotations

import os
import re

SHELLS = ("posix", "powershell", "cmd")

# Plain in every shell: Unicode word characters plus . + = -. Excludes , and @
# (PowerShell arrays / splatting), ~ { } $ ` and every other metacharacter.
_BARE = re.compile(r"[\w.+=-]+")
# PowerShell accepts these four as single quotes; doubling any of them escapes it.
_PS_QUOTES = re.compile("['‘’‚‛]")


def quote(arg: str, shell: str) -> str | None:
    """`arg` as one literal word for `shell`, or None if it can't be made safe."""
    if _BARE.fullmatch(arg):
        return arg
    if shell == "posix":
        return "'" + arg.replace("'", "'\\''") + "'"
    if shell == "powershell":
        return "'" + _PS_QUOTES.sub(lambda m: m.group() * 2, arg) + "'"
    if shell == "cmd":
        # Inside "..." cmd.exe keeps & | < > ^ ( ) literal but still expands %VAR%
        # (and !VAR! with delayed expansion); names can't contain '"'.
        if any(ch in arg for ch in '%!"'):
            return None
        return f'"{arg}"'
    raise ValueError(f"unknown shell {shell!r}")


def quote_all(arg: str) -> dict[str, str | None]:
    return {shell: quote(arg, shell) for shell in SHELLS}


def command(verb: str, names: list[str], shell: str | None = None) -> str | None:
    """`nightward <verb> <names...>` safe to paste into `shell` (default: this OS's)."""
    shell = shell or ("powershell" if os.name == "nt" else "posix")
    args = [quote(n, shell) for n in names]
    if any(a is None for a in args):
        return None
    # A leading "-" would be read as an option, and quoting can't prevent that.
    sep = ["--"] if any(n.startswith("-") for n in names) else []
    return " ".join(["nightward", verb, *sep, *args])
