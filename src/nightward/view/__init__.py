"""nightward view — generate a static, read-only blast-radius dashboard.

Design (see docs/superpowers/specs/2026-06-05-nightward-view-dashboard-design.md):
the generator NEVER injects captured data into HTML. It copies static assets
(index.html / app.js / style.css) verbatim and writes the data to a sibling
``data.json``. The page ``fetch``es that JSON and renders via ``textContent``,
so there is no server-side template-injection surface. ``fetch`` is blocked on
``file://`` by CORS, hence local viewing goes through ``nightward view --serve``.
"""
from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

from ..core.baseline import Store
from ..runner import is_stale
from ..shellquote import SHELLS, quote, quote_all

ASSETS = Path(__file__).parent / "assets"
STATIC_FILES = ("index.html", "app.js", "style.css")
DEFAULT_STORE = ".nightward"


def run_command(nightward_dir: Path | str) -> dict[str, str | None]:
    """`nightward run` for this store, per shell (None: no safe form there).

    The page's "re-run" hint must work in the user's project, not name the
    README's quickstart fixture (R3-WEB-04).
    """
    src = str(nightward_dir)
    if Path(src) == Path(DEFAULT_STORE):
        return {shell: "nightward run ." for shell in SHELLS}
    out: dict[str, str | None] = {}
    for shell in SHELLS:
        q = quote(src, shell)
        out[shell] = None if q is None else f"nightward run . --dir {q}"
    return out


def collect_data(nightward_dir: Path | str) -> dict:
    """Read a .nightward store into the {report, meta} payload the page renders.

    Tolerates a missing store (no run yet) by returning report=None — the page
    has a first-class "no report" state for exactly this.
    """
    src = Path(nightward_dir)
    store = Store(src)
    report = store.load_report()        # None if no run; NightwardError if corrupt
    run_meta = store.load_run_meta()
    baseline = store.load_baseline()    # {} if absent
    pending = store.load_pending()
    blast = (report or {}).get("blast_radius", {})
    # Group names too: the group chip is `approve --group G` (R3-DATA-06).
    names = {it["name"] for items in blast.values() for it in items} | set(blast)
    names |= {it["name"] for it in (report or {}).get("judged_same") or []}   # reject chips
    return {
        "report": report,
        # Copy-paste commands use these per-shell forms, never the raw name: a
        # name from test code must not run code in the approver's shell (R2-WEB-02).
        "quoted": {n: quote_all(n) for n in sorted(names)},
        "meta": {
            "skipped": run_meta.get("skipped", 0),
            "failed": run_meta.get("failed", 0),
            "errors": run_meta.get("errors", 0),
            "judge": run_meta.get("judge"),
            "baseline_count": len(baseline),
            "pending_count": len(pending),
            # baseline, capture or rejections moved since the report: its verdict is void
            "stale": is_stale(store, report),
            "source": str(src),
            "run_command": run_command(src),
            # when this page was built, with its offset like report.generated_at
            "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    }


def build_site(nightward_dir: Path | str, out_dir: Path | str) -> Path:
    """Emit a self-contained static dashboard into ``out_dir``; return its path."""
    out = Path(out_dir)
    data = collect_data(nightward_dir)

    out.mkdir(parents=True, exist_ok=True)
    # Binary copy preserves the assets' UTF-8 bytes (and their <meta charset>).
    for name in STATIC_FILES:
        shutil.copyfile(ASSETS / name, out / name)
    # ensure_ascii=False keeps Hangul as real UTF-8 bytes; it lives in a .json
    # file served as application/json, so it never reaches an HTML parser.
    (out / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return out
