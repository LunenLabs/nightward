"""Make look-alike text changes visible to the human reviewer (display only).

A CLDR upgrade turns NO-BREAK SPACE into NARROW NO-BREAK SPACE in every
formatted price; a template adds a ZERO WIDTH SPACE; a Cyrillic "а" replaces a
Latin "a". The fingerprint catches all of these, but the -/+ lines look the
same, so a reviewer can't tell what to approve (R2-FIN-04). When two texts
are equal once invisible and look-alike characters are folded away, `reveal`
escapes exactly the characters that differ and names them. Fingerprints and
stored payloads never pass through here.
"""
from __future__ import annotations

import difflib
import unicodedata

# Look-alikes that NFKC keeps apart: Cyrillic/Greek letters shaped like Latin
# ones, and the dashes/quotes that pass for ASCII in most fonts.
_LOOKALIKE = str.maketrans({
    **dict(zip("аеорсухіјѕԁһ", "aeopcyxijsdh", strict=True)),
    **dict(zip("АВЕКМНОРСТХІЈЅ", "ABEKMHOPCTXIJS", strict=True)),
    **dict(zip("οΑΒΕΖΗΙΚΜΝΟΡΤΥΧν", "oABEZHIKMNOPTYXv", strict=True)),
    **dict.fromkeys("‐‑‒–−", "-"),
    **dict.fromkeys("‘’", "'"),
    **dict.fromkeys("“”", '"'),
})
_MAX_CHARS = 4000     # longer lines are only flagged, not aligned char by char
_MAX_NOTES = 3


def _invisible(ch: str) -> bool:
    cat = unicodedata.category(ch)
    return (cat in ("Cf", "Cc", "Co", "Cn") and ch not in "\t\n"
            or "︀" <= ch <= "️" or "\U000e0100" <= ch <= "\U000e01ef")


def _fold(text: str) -> str:
    """What a reader sees: no invisibles, look-alikes merged, whitespace runs as one."""
    text = unicodedata.normalize("NFKC", text).translate(_LOOKALIKE)
    return " ".join("".join(ch for ch in text if not _invisible(ch)).split())


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" "))]


def _escape(chars: str) -> str:
    return "".join(f"\\u{ord(c):04x}" if ord(c) <= 0xFFFF else f"\\U{ord(c):08x}"
                   for c in chars)


def _names(chars: str) -> str:
    if not chars:
        return "nothing"
    shown = [f"U+{ord(c):04X} {unicodedata.name(c, 'unnamed')}" for c in chars[:3]]
    return " + ".join(shown) + (" ..." if len(chars) > 3 else "")


def reveal(old: str, new: str) -> tuple[str, str, str] | None:
    """(old, new, note) with the differing characters escaped, when the two texts
    differ but look the same; None when the difference is plainly visible.

    Indentation is structure in a JSON diff, so lines indented differently are
    never treated as look-alikes.
    """
    if old == new or _indent(old) != _indent(new) or _fold(old) != _fold(new):
        return None
    if len(old) > _MAX_CHARS or len(new) > _MAX_CHARS:
        return old, new, "differs only in invisible or look-alike characters"
    a: list[str] = []
    b: list[str] = []
    notes: list[str] = []
    ops = difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            a.append(old[i1:i2])
            b.append(new[j1:j2])
            continue
        a.append(_escape(old[i1:i2]))
        b.append(_escape(new[j1:j2]))
        note = f"{_names(old[i1:i2])} -> {_names(new[j1:j2])}"
        if note not in notes:
            notes.append(note)
    more = f" (+{len(notes) - _MAX_NOTES} more)" if len(notes) > _MAX_NOTES else ""
    return "".join(a), "".join(b), "; ".join(notes[:_MAX_NOTES]) + more
