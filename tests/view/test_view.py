"""nightward view — static dashboard generator.

These tests are the persona panel's MUST-FIX list, frozen:
  P6 (Korean dev) + Security: UTF-8 file writes, <meta charset>, CSP, no innerHTML,
     Hangul preserved as bytes, survives cp949-unencodable chars.
  P9 (new user): all four states (no-report / no-baseline / intact / breached) build
     without crashing.
  P1 (solo dev): data.json carries the meta (counts, generated) the header needs.

If one fails, it is a real defect — fix the generator, do not weaken the test.
"""
import json
import os
import subprocess
import sys

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior, canonical_json
from nightward.view import build_site

# --- helpers ---------------------------------------------------------------


def _seed(tw, *, baseline=None, pending=None, report=None, meta=None):
    """Populate a .nightward store on disk and return its Store."""
    store = Store(tw)
    store.ensure()
    for b in baseline or []:
        store._file(store.baseline_dir, b.name, "approved").write_text(
            canonical_json(b.to_dict()), encoding="utf-8"
        )
    for b in pending or []:
        store.write_pending(b)
    if report is not None:
        store.write_report(report)
    if meta is not None:
        store.write_run_meta(meta)
    return store


def _breached_report():
    return {
        "boundary": "breached",
        "unapproved": 1,
        "counts": {"total": 1, "unchanged": 0, "new": 0, "changed": 1, "removed": 0},
        "blast_radius": {
            "billing": [
                {
                    "name": "checkout_total",
                    "kind": "CHANGED",
                    "group": "billing",
                    "diff": ("--- approved\n+++ received\n@@ -1 +1 @@\n"
                             '-  "total": 28.05\n+  "total": 99.99'),
                }
            ]
        },
    }


# --- emit / structure ------------------------------------------------------


def test_build_site_emits_all_assets(tmp_path):
    _seed(tmp_path / ".nightward", report=_breached_report(), meta={"skipped": 0, "failed": 0})
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    for name in ("index.html", "app.js", "style.css", "data.json"):
        assert (out / name).exists(), f"{name} missing"


def test_index_html_has_charset_and_csp(tmp_path):
    _seed(tmp_path / ".nightward", report=_breached_report())
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    raw = (out / "index.html").read_bytes()
    assert b'<meta charset="utf-8">' in raw
    assert b"Content-Security-Policy" in raw
    # inline script must be forbidden -> data goes through app.js, not inline
    assert b"script-src 'self'" in raw


def test_app_js_has_no_innerhtml(tmp_path):
    """Security P0: rendering must use textContent, never innerHTML."""
    _seed(tmp_path / ".nightward", report=_breached_report())
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    js = (out / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js


def test_data_json_structure(tmp_path):
    _seed(
        tmp_path / ".nightward",
        baseline=[Behavior("checkout_total", {"total": 28.05}, group="billing")],
        report=_breached_report(),
        meta={"skipped": 2, "failed": 0},
    )
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert set(data) >= {"report", "meta"}
    assert data["report"]["boundary"] == "breached"
    m = data["meta"]
    assert m["skipped"] == 2
    assert m["baseline_count"] == 1
    assert "generated" in m
    assert "source" in m


# --- Korean / encoding (P6) ------------------------------------------------


def test_data_json_preserves_hangul_bytes(tmp_path):
    report = {
        "boundary": "breached",
        "unapproved": 1,
        "counts": {"total": 1, "unchanged": 0, "new": 1, "changed": 0, "removed": 0},
        "blast_radius": {
            "결제": [
                {"name": "결제_합계_계산", "kind": "NEW", "group": "결제",
                 "diff": "+ 주문이 완료되었습니다"}
            ]
        },
    }
    _seed(tmp_path / ".nightward", report=report)
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    raw = (out / "data.json").read_bytes()
    assert "결제_합계_계산".encode() in raw  # Hangul survives as UTF-8, not \uXXXX


def test_build_site_survives_cp949_unencodable(tmp_path):
    """Emoji + Hangul (not in cp949) must not crash the generator on Windows."""
    report = {
        "boundary": "breached",
        "unapproved": 1,
        "counts": {"total": 1, "unchanged": 0, "new": 1, "changed": 0, "removed": 0},
        "blast_radius": {
            "i18n": [{"name": "greeting", "kind": "NEW", "group": "i18n",
                      "diff": "+ 완료 😀 …"}]
        },
    }
    _seed(tmp_path / ".nightward", report=report)
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    raw = (out / "data.json").read_bytes()
    assert "😀".encode() in raw


# --- empty states (P9) -----------------------------------------------------


def test_build_site_no_report(tmp_path):
    """No run yet -> still produce a valid site; report is null."""
    Store(tmp_path / ".nightward").ensure()
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert data["report"] is None
    assert (out / "index.html").exists()


def test_build_site_no_baseline_signaled(tmp_path):
    _seed(tmp_path / ".nightward", report=_breached_report())
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert data["meta"]["baseline_count"] == 0


def test_build_site_intact(tmp_path):
    report = {"boundary": "intact", "unapproved": 0,
              "counts": {"total": 2, "unchanged": 2, "new": 0, "changed": 0, "removed": 0},
              "blast_radius": {}}
    _seed(tmp_path / ".nightward", report=report)
    out = build_site(tmp_path / ".nightward", tmp_path / "site")
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert data["report"]["boundary"] == "intact"


def test_missing_source_dir_is_clean_error(tmp_path):
    """Pointing at a non-existent store should not traceback."""
    from nightward.errors import NightwardError

    try:
        build_site(tmp_path / "nope", tmp_path / "site")
    except NightwardError:
        pass  # clean error is acceptable
    else:
        # or it builds a no-report site — also acceptable, but must not crash
        assert (tmp_path / "site" / "index.html").exists()


# --- CLI integration -------------------------------------------------------


def _cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )


def test_cli_view_builds_site(tmp_path):
    _seed(tmp_path / ".nightward", report=_breached_report())
    r = _cli("view", "--dir", str(tmp_path / ".nightward"),
             "--out", str(tmp_path / "site"), "--no-serve", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "site" / "index.html").exists()


def test_cli_view_cp949_stdout_no_crash(tmp_path):
    report = {
        "boundary": "breached", "unapproved": 1,
        "counts": {"total": 1, "unchanged": 0, "new": 1, "changed": 0, "removed": 0},
        "blast_radius": {"결제": [{"name": "결제_합계", "kind": "NEW", "group": "결제",
                                   "diff": "+ 완료"}]},
    }
    _seed(tmp_path / ".nightward", report=report)
    env = dict(os.environ, PYTHONIOENCODING="cp949")
    r = _cli("view", "--dir", str(tmp_path / ".nightward"),
             "--out", str(tmp_path / "site"), "--no-serve", cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr
