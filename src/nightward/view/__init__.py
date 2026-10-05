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

ASSETS = Path(__file__).parent / "assets"
STATIC_FILES = ("index.html", "app.js", "style.css")


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
    return {
        "report": report,
        "meta": {
            "skipped": run_meta.get("skipped", 0),
            "failed": run_meta.get("failed", 0),
            "errors": run_meta.get("errors", 0),
            "judge": run_meta.get("judge"),
            "baseline_count": len(baseline),
            "pending_count": len(pending),
            # baseline or capture moved since the report: its verdict is void
            "stale": is_stale(store, report),
            "source": str(src),
            "generated": datetime.datetime.now().isoformat(timespec="seconds"),
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
