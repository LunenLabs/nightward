"""The clean-room demo site is the only dashboard ever published (Pages, README
screenshots). It must not carry anything from the machine that built it."""
import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_demo_site_names_no_local_path(tmp_path, monkeypatch):
    out = tmp_path / "site"
    monkeypatch.setattr(sys, "argv", ["build_demo.py", str(out)])
    runpy.run_path(str(ROOT / "scripts" / "build_demo.py"), run_name="__main__")

    meta = json.loads((out / "data.json").read_text(encoding="utf-8"))["meta"]
    assert meta["source"] == ".nightward"
    assert set(meta["run_command"].values()) == {"nightward run ."}   # every shell
    text = (out / "data.json").read_text(encoding="utf-8")
    assert str(Path.home()) not in text and "Temp" not in text
