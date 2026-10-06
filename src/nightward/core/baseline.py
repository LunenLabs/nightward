"""Store — the on-disk golden set.

Layout (git-native, approvaltests-style):
    .nightward/
      baseline/<name>.approved.json    # committed — the regression boundary
      pending/<name>.received.json     # gitignored — this run's observed behavior
      rejected/<name>.rejected.json    # committed — confirmed regressions (approve --all skips)
      report.json                      # last blast radius (+ digests of what it compared)
      run_meta.json                    # last run's counts, run token, judge spec
      reviewed.json                    # the capture a human last saw (approve checks it)

Every name-to-path mapping goes through `_file`, which validates the name, so
no CLI argument can address a file outside the store.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path

from ..errors import NightwardError
from .behavior import Behavior, canonical_json, validate_name


def _atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file + os.replace, so readers never see a torn file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def digest(behaviors: dict[str, Behavior]) -> str:
    """Identity of a behavior set (baseline or pending): changes iff any behavior does."""
    h = hashlib.sha256()
    for name, b in sorted(behaviors.items()):
        h.update(canonical_json([name, b.group, b.fingerprint()]).encode("utf-8"))
    return h.hexdigest()


def _file_text(b: Behavior) -> str:
    # Trailing newline: git diffs and end-of-file-fixer hooks expect one. Layout
    # only - fingerprints hash the payload, so existing baselines stay valid.
    return canonical_json(b.to_dict()) + "\n"


def _read_json(path: Path) -> object:
    """Parse a store file; any unreadable content becomes a NightwardError."""
    try:
        text = path.read_text(encoding="utf-8")
        return json.loads(text)
    except UnicodeDecodeError as exc:
        raise NightwardError(f"corrupt file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        if _CONFLICT.search(text):
            raise NightwardError(_conflict_message(path)) from exc
        raise NightwardError(f"corrupt file {path}: {exc}") from exc


# git's conflict markers at the start of a line (both sides approved differently).
_CONFLICT = re.compile(r"^(<{7}|>{7})( |$)", re.M)


def _conflict_message(path: Path) -> str:
    msg = (f"{path} has unresolved merge conflict markers - keep one side "
           f"(`git checkout --ours -- {path}` or `--theirs`), then `nightward run`")
    for suffix in (".approved.json", ".received.json", ".rejected.json"):
        if path.name.endswith(suffix):
            name = path.name[:-len(suffix)]
            return msg + f" and `nightward approve {name}` if the result should stand"
    return msg


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.baseline_dir = self.root / "baseline"
        self.pending_dir = self.root / "pending"
        self.rejected_dir = self.root / "rejected"
        self.report_path = self.root / "report.json"
        self.meta_path = self.root / "run_meta.json"
        self.reviewed_path = self.root / "reviewed.json"

    def ensure(self) -> None:
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _file(dir_: Path, name: str, suffix: str) -> Path:
        return dir_ / f"{validate_name(name)}.{suffix}.json"

    # ---- pending (this run) --------------------------------------------
    def write_pending(self, b: Behavior) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self._file(self.pending_dir, b.name, "received").write_text(
            _file_text(b), encoding="utf-8"
        )

    def clear_pending(self) -> None:
        if self.pending_dir.exists():
            for f in self.pending_dir.glob("*.received.json"):
                f.unlink()

    def replace_pending(self, behaviors: Iterable[Behavior]) -> None:
        """Swap in a complete new capture: build it aside, then replace pending/.

        A crash mid-write leaves the previous capture intact instead of a partial
        one (which would surface as mass false REMOVED).
        """
        staging = self.root / "pending.tmp"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            for b in behaviors:
                self._file(staging, b.name, "received").write_text(
                    _file_text(b), encoding="utf-8"
                )
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if self.pending_dir.exists():
            shutil.rmtree(self.pending_dir)
        staging.rename(self.pending_dir)

    # ---- loading -------------------------------------------------------
    def _load_dir(self, dir_: Path, suffix: str) -> dict[str, Behavior]:
        out: dict[str, Behavior] = {}
        if not dir_.exists():
            return out
        for f in sorted(dir_.glob(f"*.{suffix}.json")):
            data = _read_json(f)  # its error already names the file
            try:
                b = Behavior.from_dict(data)
            except NightwardError as exc:
                raise NightwardError(f"corrupt behavior file {f}: {exc}") from exc
            out[b.name] = b
        return out

    def load_baseline(self) -> dict[str, Behavior]:
        return self._load_dir(self.baseline_dir, "approved")

    def load_pending(self) -> dict[str, Behavior]:
        return self._load_dir(self.pending_dir, "received")

    def load_rejected(self) -> dict[str, Behavior]:
        return self._load_dir(self.rejected_dir, "rejected")

    # ---- decisions -----------------------------------------------------
    def approve(self, name: str) -> None:
        """Promote a pending behavior into the baseline (add or change)."""
        src = self._file(self.pending_dir, name, "received")
        if not src.exists():
            raise NightwardError(f"no pending behavior named {name!r} to approve")
        self.baseline_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._file(self.baseline_dir, name, "approved"),
                      src.read_text(encoding="utf-8"))

    def refresh_source(self, name: str, source: str) -> None:
        """Record the test that now captures an approved behavior (D13).

        Removal evidence only: the payload, group and fingerprint stay as approved.
        """
        path = self._file(self.baseline_dir, name, "approved")
        b = Behavior.from_dict(_read_json(path))
        _atomic_write(path, _file_text(dataclasses.replace(b, source=source)))

    def approve_removal(self, name: str) -> None:
        """Accept that a behavior is gone: drop it from the baseline."""
        dst = self._file(self.baseline_dir, name, "approved")
        if not dst.exists():
            raise NightwardError(f"no baseline behavior named {name!r} to remove")
        dst.unlink()

    def mark_rejected(self, name: str) -> None:
        """Record a confirmed regression. Audit only — the baseline is untouched.

        The recorded snapshot is the received behavior, or (for a regression
        that *removed* a behavior) the approved one that went missing.
        """
        src = self._file(self.pending_dir, name, "received")
        if not src.exists():
            src = self._file(self.baseline_dir, name, "approved")
        if not src.exists():
            raise NightwardError(f"no pending or baseline behavior named {name!r} to reject")
        self.rejected_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self._file(self.rejected_dir, name, "rejected"),
                      src.read_text(encoding="utf-8"))

    def clear_rejection(self, name: str) -> bool:
        """Drop a rejection record (an explicit approve overrides it)."""
        f = self._file(self.rejected_dir, name, "rejected")
        if not f.exists():
            return False
        f.unlink()
        return True

    # ---- report --------------------------------------------------------
    def write_report(self, report: dict) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.report_path, json.dumps(report, indent=2, ensure_ascii=False))

    def invalidate_report(self) -> None:
        """Drop the last report: it no longer describes the store (fail closed)."""
        self.report_path.unlink(missing_ok=True)

    def load_report(self) -> dict | None:
        if not self.report_path.exists():
            return None
        report = _read_json(self.report_path)
        if not isinstance(report, dict):
            raise NightwardError(f"corrupt file {self.report_path}: expected a JSON object")
        return report

    # ---- what a human last saw (D10) ------------------------------------
    def mark_reviewed(self, pending_digest: str, via: str) -> None:
        _atomic_write(self.reviewed_path, json.dumps(
            {"pending_digest": pending_digest, "via": via}))

    def load_reviewed(self) -> dict:
        if not self.reviewed_path.exists():
            return {}
        try:
            mark = _read_json(self.reviewed_path)
        except NightwardError:
            return {}
        return mark if isinstance(mark, dict) else {}

    # ---- run metadata (skipped/failed counts from the last run) ---------
    def write_run_meta(self, meta: dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.meta_path, json.dumps(meta))

    def load_run_meta(self) -> dict:
        # Advisory only (warning counts, judge spec): unreadable -> treat as absent.
        if not self.meta_path.exists():
            return {}
        try:
            meta = _read_json(self.meta_path)
        except NightwardError:
            return {}
        return meta if isinstance(meta, dict) else {}
