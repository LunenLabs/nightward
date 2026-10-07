"""Beta round 2: gate-integrity defects, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import subprocess
import sys

import pytest

from nightward import mcp_server
from nightward.errors import NightwardError
from nightward.runner import execute_run


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def status_json(tmp_path, tw):
    return json.loads(cli("status", "--json", "--dir", str(tw), cwd=tmp_path).stdout)


APP = 'def replicas(env):\n    return {"dev": 1, "prod": 4}[env]\n'
TEST_APP = ('from app import replicas\n'
            'def test_replicas(behavior):\n'
            '    behavior("replicas", {e: replicas(e) for e in ["dev", "prod"]}, group="p")\n')


@pytest.fixture
def approved_app(tmp_path):
    write(tmp_path / "app.py", APP)
    write(tmp_path / "test_app.py", TEST_APP)
    tw = tmp_path / ".tw"
    assert cli("run", ".", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0
    return tmp_path, tw


# ---- R2-OPS-01 (D12): an aborted run never leaves the old green report ---------

@pytest.mark.parametrize("break_it", [
    lambda p: write(p / "app.py", "import yaml_missing_dep\n" + APP),     # exit 2
    lambda p: (p / "test_app.py").unlink(),                              # exit 5
    lambda p: write(p / "conftest.py", "import missing_plugin_dep\n"),   # exit 4
])
def test_aborted_run_invalidates_the_report(approved_app, break_it):
    tmp_path, tw = approved_app
    break_it(tmp_path)
    r = cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2
    assert "report was invalidated" in r.stderr
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode != 0
    assert status_json(tmp_path, tw)["boundary"] == "unknown"
    assert mcp_server.status_tool(str(tw))["boundary"] == "unknown"
    assert (tw / "pending" / "replicas.received.json").exists()   # capture untouched


def test_timed_out_run_invalidates_the_report(approved_app):
    tmp_path, tw = approved_app
    write(tmp_path / "test_slow.py", "import time\ndef test_slow():\n    time.sleep(30)\n")
    with pytest.raises(NightwardError, match="timed out"):
        execute_run(str(tmp_path), str(tw), capture_output=True, timeout=3)
    assert not (tw / "report.json").exists()


def test_run_on_a_vanished_path_invalidates_the_report(approved_app):
    tmp_path, tw = approved_app
    r = cli("run", "tests_renamed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2 and "does not exist" in r.stderr
    assert status_json(tmp_path, tw)["boundary"] == "unknown"


# ---- R2-OPS-03 (D11): one writer per store -------------------------------------

def test_second_writer_fails_fast_while_the_store_is_locked(approved_app):
    from nightward.core.lock import store_lock
    tmp_path, tw = approved_app
    before = (tw / "pending" / "replicas.received.json").read_bytes()
    with store_lock(tw, "nightward run"):
        for args in (("run", "."), ("approve", "--all"), ("reject", "replicas")):
            r = cli(*args, "--dir", str(tw), cwd=tmp_path)
            assert r.returncode == 2, (args, r.stdout, r.stderr)
            assert "another nightward process" in r.stderr and "nightward run" in r.stderr
        with pytest.raises(NightwardError, match="another nightward process"):
            mcp_server.run_tool(str(tmp_path), str(tw))
    assert (tw / "pending" / "replicas.received.json").read_bytes() == before
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert not (tw / ".lock").exists()


def test_direct_plugin_flush_refuses_a_locked_store(approved_app):
    from nightward.core.lock import store_lock
    tmp_path, tw = approved_app
    write(tmp_path / "app.py", APP.replace('"dev": 1', '"dev": 0'))
    with store_lock(tw, "nightward run"):
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "--nightward-record", "--nightward-dir", str(tw)],
                           cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode != 0
    assert "another nightward process" in r.stdout + r.stderr
    got = json.loads((tw / "pending" / "replicas.received.json").read_text("utf-8"))
    assert got["payload"]["dev"] == 1


def test_stale_lock_of_a_dead_process_is_taken_over(approved_app):
    tmp_path, tw = approved_app
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True)
    import socket
    write(tw / ".lock", json.dumps({"pid": int(dead.stdout), "host": socket.gethostname(),
                                    "command": "nightward run", "since": "earlier"}))
    r = cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tw / ".lock").exists()


PARAMS = ('import os, pytest\n'
          'ENV = os.environ.get("TOXENV", "py")\n'
          '@pytest.mark.parametrize("i", range(200))\n'
          'def test_c(behavior, i):\n'
          '    behavior(f"cfg.{i:03d}", {"i": i, "py": ENV if i == 7 else "any"}, group="g")\n')


def test_concurrent_runs_never_report_clobbered_removals(tmp_path):
    write(tmp_path / "test_c.py", PARAMS)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    procs = [subprocess.Popen([sys.executable, "-m", "nightward", "run", ".", "--dir", str(tw)],
                              cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env={**os.environ, "TOXENV": env}, text=True,
                              encoding="utf-8", errors="replace")
             for env in ("py", "py312")]
    outs = [(*p.communicate(timeout=300), p.returncode) for p in procs]
    for out, err, code in outs:
        assert "REMOVED" not in out
        assert code in (0, 1) or "another nightward process" in err, err
    assert len(list((tw / "pending").glob("*.json"))) == 200


# ---- R2-WEB-01 (D10): approve exactly what was reviewed --------------------------

SHOP = ('LABEL = "Total"\nUNIT = 10\ndef checkout(qty):\n'
        '    return {"label": LABEL, "qty": qty, "total": qty * UNIT}\n')
TEST_SHOP = ('from shop import checkout\n'
             'def test_checkout(behavior):\n'
             '    behavior("checkout.3", checkout(3), group="billing")\n')


@pytest.fixture
def reviewed_then_agent_ran(tmp_path, monkeypatch):
    """Human reviewed the label change; the agent then ran again with a price bug."""
    write(tmp_path / "shop.py", SHOP)
    write(tmp_path / "test_shop.py", TEST_SHOP)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "shop.py", SHOP.replace('"Total"', '"Order total"'))
    mcp_server.run_tool(".", str(tw))                              # agent
    review = cli("review", "--dir", str(tw), cwd=tmp_path)        # human
    assert "Order total" in review.stdout and "36" not in review.stdout
    write(tmp_path / "shop.py", SHOP.replace('"Total"', '"Order total"').replace("10", "12"))
    mcp_server.run_tool(".", str(tw))                              # agent again
    return tmp_path, tw


@pytest.mark.parametrize("args", [("checkout.3",), ("--all",)])
def test_approve_refuses_a_capture_changed_since_review(reviewed_then_agent_ran, args):
    tmp_path, tw = reviewed_then_agent_ran
    r = cli("approve", *args, "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2, r.stdout
    assert "changed since" in r.stderr and "nightward review" in r.stderr
    base = json.loads((tw / "baseline" / "checkout.3.approved.json").read_text("utf-8"))
    assert base["payload"]["total"] == 30


def test_approve_after_reviewing_again_promotes_what_was_shown(reviewed_then_agent_ran):
    tmp_path, tw = reviewed_then_agent_ran
    review = cli("review", "--dir", str(tw), cwd=tmp_path)
    assert "36" in review.stdout
    r = cli("approve", "checkout.3", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    fp = json.loads((tw / "pending" / "checkout.3.received.json").read_text("utf-8"))
    assert "approved checkout.3 (" in r.stdout
    assert json.loads((tw / "baseline" / "checkout.3.approved.json").read_text("utf-8")) == fp


def test_approve_refuses_when_only_an_agent_has_run(tmp_path, monkeypatch):
    write(tmp_path / "shop.py", SHOP)
    write(tmp_path / "test_shop.py", TEST_SHOP)
    monkeypatch.chdir(tmp_path)
    mcp_server.run_tool(".", ".tw")
    r = cli("approve", "--all", "--dir", ".tw", cwd=tmp_path)
    assert r.returncode == 2 and "nightward review" in r.stderr
    assert cli("view", "--no-serve", "--dir", ".tw", cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", "--dir", ".tw", cwd=tmp_path).returncode == 0


# ---- R2-OPS-02 (D13): removal proof needs a whole-suite run and current sources --

MOVED_V1 = ('def test_one(behavior):\n'
            '    behavior("render.dev", "kind: Deployment\\nreplicas: 1\\n", group="render")\n'
            '    behavior("render.meta", {"api": "apps/v1"}, group="render")\n')
MOVED_V2 = ('import os, pytest\n'
            'def test_one(behavior):\n'
            '    behavior("render.meta", {"api": "apps/v1"}, group="render")\n'
            '@pytest.mark.skipif(os.environ.get("CI_NO_K8S") == "1", reason="no schema")\n'
            'def test_dev(behavior):\n'
            '    behavior("render.dev", "kind: Deployment\\nreplicas: 1\\n", group="render")\n')


def test_moved_capture_is_not_proven_removed_by_its_old_test(tmp_path):
    write(tmp_path / "test_a.py", MOVED_V1)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_a.py", MOVED_V2)
    assert cli("run", ".", "--dir", str(tw), cwd=tmp_path).returncode == 0   # intact
    cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"CI_NO_K8S": "1"})
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tw / "baseline" / "render.dev.approved.json").exists()
    assert "1 skipped" in r.stdout     # D18: the run was not clean


def test_approve_all_backfills_sources_of_unchanged_behaviors(tmp_path):
    write(tmp_path / "test_a.py", MOVED_V1)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_a.py", MOVED_V2)
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "refreshed" in r.stdout
    dev = json.loads((tw / "baseline" / "render.dev.approved.json").read_text("utf-8"))
    assert dev["source"] == "test_a.py::test_dev"


LEGACY = {"test_a.py": 'def test_x(behavior):\n    behavior("x", 1, group="g")\n',
          "test_b.py": ('def test_y(behavior):\n    behavior("y", 2, group="g")\n'
                        'def test_z(behavior):\n    behavior("z", 3, group="g")\n')}


@pytest.fixture
def legacy_store(tmp_path):
    """A v0.2.0-style store: no `source` in the baseline, no run history."""
    for name, body in LEGACY.items():
        write(tmp_path / name, body)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    for f in (tw / "baseline").glob("*.json"):
        data = json.loads(f.read_text("utf-8"))
        data.pop("source", None)
        f.write_text(json.dumps(data), encoding="utf-8")
    (tw / "run_meta.json").unlink()
    return tmp_path, tw


@pytest.mark.parametrize("path", ["test_a.py", "test_b.py::test_y"])
def test_legacy_partial_path_proves_no_removal(legacy_store, path):
    tmp_path, tw = legacy_store
    cli("run", path, "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert len(list((tw / "baseline").glob("*.json"))) == 3
    assert "whole-suite" in r.stdout


def test_legacy_whole_suite_run_proves_removal(legacy_store):
    tmp_path, tw = legacy_store
    write(tmp_path / "test_b.py", 'def test_y(behavior):\n    behavior("y", 2, group="g")\n'
                                  'def test_z(behavior):\n    pass\n')
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tw / "baseline" / "z.approved.json").exists()


# ---- R2-COORD-01 (D17): rejections are shared decisions ------------------------

def test_init_does_not_ignore_rejections_and_drops_the_legacy_rule(tmp_path):
    write(tmp_path / ".gitignore", "node_modules/\n.nightward/rejected/\n")
    r = cli("init", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".nightward/rejected/" not in gi and "node_modules/" in gi
    assert ".nightward/pending/" in gi
    assert "rejected/" in r.stdout          # tells the user why the line went


def test_rejection_protects_a_fresh_clone(tmp_path):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
    write(origin / "app.py", APP)
    write(origin / "test_app.py", TEST_APP)
    subprocess.run(["git", "init", "-q"], cwd=origin, check=True)
    assert cli("init", cwd=origin).returncode == 0
    cli("run", ".", cwd=origin)
    cli("approve", "--all", cwd=origin)
    write(origin / "app.py", APP.replace('"dev": 1', '"dev": 0'))     # regression
    cli("run", ".", cwd=origin)
    assert cli("reject", "replicas", cwd=origin).returncode == 0
    subprocess.run(["git", "add", "-A"], cwd=origin, check=True)
    subprocess.run([*git, "commit", "-qm", "reject"], cwd=origin, check=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    assert (clone / ".nightward" / "rejected" / "replicas.rejected.json").exists()
    cli("run", ".", cwd=clone)
    r = cli("approve", "--all", cwd=clone)
    assert "kept (rejected)" in r.stdout
    assert cli("gate", cwd=clone).returncode == 1


# ---- R2-DATA-03: an incomplete run never prints "Boundary: intact" ---------------

def test_incomplete_run_summary_says_incomplete(tmp_path):
    write(tmp_path / "test_m.py",
          'import os\n'
          'def test_revenue(behavior):\n'
          '    behavior("revenue_total", {"total": 1234.5}, group="billing")\n'
          'if os.environ.get("PR") == "1":\n'
          '    def test_new_metric(behavior):\n'
          '        behavior("segment_mean", {"mean": float("nan")}, group="billing")\n')
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    r = cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"PR": "1"})
    assert r.returncode == 1
    assert "Boundary: incomplete (1 failed capture test" in r.stdout, r.stdout
    assert "intact" not in r.stdout


# ---- R2-DATA-04: a verdict from an existing capture; pytest-arg passthrough ------

TEST_X = ('import os, pytest\n'
          'def test_x(behavior):\n'
          '    behavior("x", {"v": int(os.environ.get("V", "1"))}, group="g")\n'
          '@pytest.mark.slow\n'
          'def test_slow(behavior):\n'
          '    behavior("slow", 1, group="g")\n'
          'def test_bad(behavior):\n'
          '    if os.environ.get("BAD"):\n'
          '        raise RuntimeError("boom")\n')


@pytest.fixture
def approved_x(tmp_path):
    write(tmp_path / "test_x.py", TEST_X)
    write(tmp_path / "pytest.ini", "[pytest]\nmarkers =\n    slow: slow\n")
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    return tmp_path, tw


def record(tmp_path, tw, *extra, env=None):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "--nightward-record", "--nightward-dir", str(tw), *extra],
                          cwd=str(tmp_path), capture_output=True, text=True,
                          env={**os.environ, **(env or {})})


def test_report_turns_a_plugin_capture_into_a_verdict(approved_x):
    tmp_path, tw = approved_x
    assert record(tmp_path, tw, env={"V": "2"}).returncode == 0
    r = cli("report", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "breached" in r.stdout and "x" in r.stdout
    gate = cli("gate", "--dir", str(tw), cwd=tmp_path)
    assert gate.returncode == 1 and "breached" in gate.stdout     # a verdict, not "stale"
    assert cli("approve", "x", "--dir", str(tw), cwd=tmp_path).returncode == 0


def test_report_of_an_incomplete_session_exits_1(approved_x):
    tmp_path, tw = approved_x
    record(tmp_path, tw, env={"BAD": "1"})
    r = cli("report", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 1 and "incomplete" in r.stdout + r.stderr


def test_report_refuses_a_capture_no_session_recorded(approved_x):
    tmp_path, tw = approved_x
    record(tmp_path, tw, env={"V": "2"})
    f = tw / "pending" / "x.received.json"
    f.write_text(f.read_text("utf-8").replace('"v": 2', '"v": 3'), encoding="utf-8")
    r = cli("report", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2 and "does not match" in r.stderr
    (tw / "run_meta.json").unlink()
    r = cli("report", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 2 and "pytest --nightward-record" in r.stderr


def test_narrowed_plugin_capture_proves_no_removal(approved_x):
    tmp_path, tw = approved_x
    record(tmp_path, tw, "-m", "not slow")
    cli("report", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert (tw / "baseline" / "slow.approved.json").exists()
    assert "narrowed" in r.stdout


def test_run_passes_pytest_args_through(approved_x):
    tmp_path, tw = approved_x
    r = cli("run", ".", "--dir", str(tw), "--", "-m", "not slow", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    meta = json.loads((tw / "run_meta.json").read_text("utf-8"))
    assert meta["deselected"] == 1 and meta["narrowed"] is True
    bad = cli("run", ".", "--dir", str(tw), "--", "--nightward-dir", "elsewhere", cwd=tmp_path)
    assert bad.returncode == 2 and "--nightward" in bad.stderr


# ---- R2-OPS-04: a store path past Windows MAX_PATH fails at capture time ---------

def test_name_too_long_for_windows_paths_fails_at_capture(tmp_path, monkeypatch):
    from nightward import pytest_plugin
    from nightward.pytest_plugin import Recorder
    monkeypatch.setattr(pytest_plugin, "_WINDOWS", True)
    deep = tmp_path / ("d" * (190 - len(str(tmp_path)))) / ".nightward"
    name = "k8s.deployment." + "very-long-service-name-" * 3 + "prod"
    with pytest.raises(NightwardError, match="too long for Windows") as exc:
        Recorder(deep).add(name, {"replicas": 3})
    assert name in str(exc.value) and "shorter" in str(exc.value)
    Recorder(deep).add("k8s.prod", {"replicas": 3})            # short names still fine
    monkeypatch.setattr(pytest_plugin, "_WINDOWS", False)
    Recorder(deep).add(name, {"replicas": 3})                  # POSIX has no such limit


# ---- R2-OPS-05: store files are LF on every OS ------------------------------------

def test_store_files_are_written_with_lf(approved_app):
    tmp_path, tw = approved_app
    write(tmp_path / "app.py", APP.replace('"dev": 1', '"dev": 2'))
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("reject", "replicas", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "replicas", "--dir", str(tw), cwd=tmp_path)
    files = [p for p in tw.rglob("*.json") if p.is_file()]
    assert any(p.parent.name == "baseline" for p in files)
    for p in files:
        assert b"\r\n" not in p.read_bytes(), p


# ---- polish: the masked-values note names behaviors and repeats only on change ----

def test_masked_note_names_behaviors_and_repeats_only_on_change(tmp_path):
    body = ('import os\n'
            'def test_t(behavior):\n'
            '    behavior("seen", {"at": "2024-01-05T09:00:00Z"}, group="g")\n'
            '    if os.environ.get("MORE"):\n'
            '        behavior("due", {"at": "2024-02-05T09:00:00Z"}, group="g")\n')
    write(tmp_path / "test_t.py", body)
    tw = tmp_path / ".tw"
    first = cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    assert "masked 1 value(s) in 1 behavior(s)" in first.stderr and "seen" in first.stderr
    again = cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    assert "masked" not in again.stderr
    more = cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"MORE": "1"})
    assert "masked 2 value(s) in 2 behavior(s)" in more.stderr and "due" in more.stderr


# The repo's own ignore files must cover every transient store entry `init`
# writes (a new transient file once showed up as untracked in every example).
def test_repo_gitignores_cover_transient_entries():
    from pathlib import Path

    from nightward.cli import LEGACY_ENTRIES, TRANSIENT_ENTRIES

    root = Path(__file__).resolve().parents[1]
    for gi in [root / ".gitignore", *sorted(root.glob("examples/*/.gitignore"))]:
        lines = gi.read_text(encoding="utf-8").splitlines()
        for entry in TRANSIENT_ENTRIES:
            assert f".nightward/{entry}" in lines, f"{gi}: missing .nightward/{entry}"
        for entry in LEGACY_ENTRIES:
            assert f".nightward/{entry}" not in lines, f"{gi}: still ignores {entry}"
