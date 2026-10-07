"""Beta round 1: gate-verdict defects reported by beta testers, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import subprocess
import sys

import pytest

from nightward import mcp_server
from nightward.core.behavior import canonical_json
from nightward.errors import NightwardError
from nightward.pytest_plugin import Recorder
from nightward.runner import execute_run
from nightward.view import collect_data


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


ENV_PRICE = ('import os\n'
             'def test_price(behavior):\n'
             '    behavior("price", int(os.environ.get("PRICE", "10")), group="billing")\n')


@pytest.fixture
def approved_price(tmp_path):
    """A project whose `price` behavior (10) is captured and approved."""
    write(tmp_path / "test_p.py", ENV_PRICE)
    tw = tmp_path / ".tw"
    assert cli("run", "test_p.py", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 0
    return tmp_path, tw


# ---- R1-WEB-01: a failed flush must never replay the previous capture ----------

def test_lone_surrogate_rejected_at_capture_naming_the_behavior():
    # R1-WEB-01: a lone surrogate passed capture and crashed the flush later.
    with pytest.raises(NightwardError, match="surrogate"):
        canonical_json({"room": "Vega \ud83d"})
    with pytest.raises(NightwardError, match="room.card"):
        Recorder().add("room.card", {"room": "Vega \ud83d"})


FLUSH_BOOM = ('import os\n'
              'from nightward.core.baseline import Store\n'
              'if os.environ.get("FLUSH_FAIL"):\n'
              '    def boom(self, behaviors):\n'
              '        raise OSError("disk full")\n'
              '    Store.replace_pending = boom\n')


def test_flush_failure_aborts_run_and_invalidates_report(approved_price):
    # R1-WEB-01: the flush crashed (exit 1) and the runner recomputed the OLD
    # pending set - a real price regression read "intact".
    tmp_path, tw = approved_price
    write(tmp_path / "conftest.py", FLUSH_BOOM)
    env = {"PRICE": "12", "FLUSH_FAIL": "1"}
    r = cli("run", "test_p.py", "--dir", str(tw), cwd=tmp_path, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "not recorded" in r.stderr
    assert "intact" not in r.stdout
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode != 0
    assert status_json(tmp_path, tw)["boundary"] != "intact"
    assert not (tw / "pending.tmp").exists()


def test_flush_failure_surfaces_through_mcp_run(approved_price, monkeypatch):
    tmp_path, tw = approved_price
    write(tmp_path / "conftest.py", FLUSH_BOOM)
    monkeypatch.setenv("FLUSH_FAIL", "1")
    monkeypatch.setenv("PRICE", "12")
    with pytest.raises(NightwardError, match="not recorded"):
        mcp_server.run_tool(str(tmp_path / "test_p.py"), str(tw))
    assert mcp_server.status_tool(str(tw))["boundary"] != "intact"


# ---- R1-FIN-01: a report older than the capture must not read "intact" ---------

def test_direct_plugin_capture_makes_report_stale(approved_price):
    # R1-FIN-01: `pytest --nightward-record` replaced pending/ without recomputing;
    # gate kept trusting the old report.json.
    tmp_path, tw = approved_price
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_p.py",
         "--nightward-record", "--nightward-dir", str(tw)],
        cwd=str(tmp_path), capture_output=True, text=True,
        env={**os.environ, "PRICE": "12"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    gate = cli("gate", "--dir", str(tw), cwd=tmp_path)
    assert gate.returncode == 1
    assert "stale" in gate.stdout
    status = status_json(tmp_path, tw)
    assert status["stale"] is True
    assert status["boundary"] == "stale"
    assert mcp_server.status_tool(str(tw))["boundary"] == "stale"


# ---- R1-OPS-06: every consumer agrees on a stale report ------------------------

def test_stale_report_is_not_intact_in_review_status_and_view(approved_price):
    tmp_path, tw = approved_price
    (tw / "baseline" / "price.approved.json").write_text(
        canonical_json({"name": "price", "group": "billing", "payload": 45}), encoding="utf-8")

    review = cli("review", "--dir", str(tw), cwd=tmp_path)
    assert review.returncode == 1
    assert "stale" in review.stdout
    assert "nothing to review" not in review.stdout

    assert status_json(tmp_path, tw)["boundary"] == "stale"
    human = cli("status", "--dir", str(tw), cwd=tmp_path)
    assert "stale" in human.stdout

    data = collect_data(tw)
    assert data["meta"]["stale"] is True


def test_fresh_report_is_not_stale_everywhere(approved_price):
    tmp_path, tw = approved_price
    assert status_json(tmp_path, tw)["boundary"] == "intact"
    assert collect_data(tw)["meta"]["stale"] is False
    review = cli("review", "--dir", str(tw), cwd=tmp_path)
    assert review.returncode == 0
    assert "nothing to review" in review.stdout


def test_successful_run_records_its_token_after_flush(tmp_path):
    # The runner's run token: a fresh run records it after a successful flush.
    write(tmp_path / "test_p.py", ENV_PRICE)
    tw = tmp_path / ".tw"
    execute_run(str(tmp_path / "test_p.py"), str(tw))
    meta = json.loads((tw / "run_meta.json").read_text(encoding="utf-8"))
    assert meta.get("run_id")


# ---- R1-DATA-02: a run with failing capture tests is incomplete, never green ---

FAILING = ('import os\n'
           'import pytest\n'
           'def test_revenue(behavior):\n'
           '    behavior("revenue_total", {"total": 1234.5}, group="billing")\n'
           'def test_new_metric(behavior):\n'
           '    behavior("segment_mean", {"mean": float("nan")}, group="billing")\n'
           '@pytest.fixture\n'
           'def cfg():\n'
           '    raise FileNotFoundError("services.toml")\n'
           'def test_cfg(behavior, cfg):\n'
           '    behavior("replicas", 3, group="g")\n')


def test_failed_capture_run_exits_1_and_gate_blocks(tmp_path):
    write(tmp_path / "test_m.py", FAILING)
    tw = tmp_path / ".tw"
    r = cli("run", "test_m.py", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Boundary:" in r.stdout                     # summary still printed
    assert "1 failed" in r.stderr and "1 error" in r.stderr

    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert report["incomplete"] == {"failed": 1, "errors": 1}

    # even once everything captured is approved, the gate stays closed
    assert cli("approve", "--all", "--dir", str(tw), cwd=tmp_path).returncode == 0
    gate = cli("gate", "--dir", str(tw), cwd=tmp_path)
    assert gate.returncode == 1
    assert "incomplete" in gate.stdout
    status = status_json(tmp_path, tw)
    assert status["incomplete"] == {"failed": 1, "errors": 1}
    assert status["boundary"] == "incomplete"


def test_failed_capture_reported_by_mcp(tmp_path):
    write(tmp_path / "test_m.py", FAILING)
    tw = tmp_path / ".tw"
    payload = mcp_server.run_tool(str(tmp_path / "test_m.py"), str(tw))
    assert payload["incomplete"] == {"failed": 1, "errors": 1}
    assert payload["boundary"] != "intact"
    assert mcp_server.status_tool(str(tw))["incomplete"] == {"failed": 1, "errors": 1}


def test_clean_run_is_complete(approved_price):
    tmp_path, tw = approved_price
    status = status_json(tmp_path, tw)
    assert status["incomplete"] is None
    assert status["boundary"] == "intact"


# ---- R1-OPS-01: approve --all never approves a standing rejection --------------

LIMITS = ('import os\n'
          'def test_limits(behavior):\n'
          '    bug = os.environ.get("BUG")\n'
          '    behavior("replica_floor", 0 if bug else 1, group="policy")\n'
          '    behavior("default_port", 8081 if bug else 8080, group="net")\n'
          '    if not os.environ.get("DROP"):\n'
          '        behavior("probe", 1, group="net")\n')


@pytest.fixture
def rejected_floor(tmp_path):
    write(tmp_path / "test_l.py", LIMITS)
    tw = tmp_path / ".tw"
    cli("run", "test_l.py", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    cli("run", "test_l.py", "--dir", str(tw), cwd=tmp_path, env={"BUG": "1"})
    assert cli("reject", "replica_floor", "--dir", str(tw), cwd=tmp_path).returncode == 0
    return tmp_path, tw


def test_approve_all_keeps_rejected_behavior(rejected_floor):
    tmp_path, tw = rejected_floor
    r = cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "kept (rejected)" in r.stdout
    assert "replica_floor" in r.stdout
    floor = json.loads((tw / "baseline" / "replica_floor.approved.json").read_text("utf-8"))
    assert floor["payload"] == 1                      # regression did not enter
    port = json.loads((tw / "baseline" / "default_port.approved.json").read_text("utf-8"))
    assert port["payload"] == 8081                    # the intended change did
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 1


def test_approve_by_name_overrides_and_clears_rejection(rejected_floor):
    tmp_path, tw = rejected_floor
    r = cli("approve", "replica_floor", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "rejection" in r.stdout
    assert not (tw / "rejected" / "replica_floor.rejected.json").exists()
    floor = json.loads((tw / "baseline" / "replica_floor.approved.json").read_text("utf-8"))
    assert floor["payload"] == 0


def test_rejection_only_holds_the_rejected_payload(rejected_floor, tmp_path):
    # A different payload later is a new change, not the rejected regression.
    tmp_path, tw = rejected_floor
    write(tmp_path / "test_l.py", LIMITS.replace("0 if bug else 1", "2"))
    cli("run", "test_l.py", "--dir", str(tw), cwd=tmp_path, env={"BUG": "1"})
    r = cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    assert "kept (rejected)" not in r.stdout
    floor = json.loads((tw / "baseline" / "replica_floor.approved.json").read_text("utf-8"))
    assert floor["payload"] == 2


def test_rejected_removal_is_kept_by_include_removed(rejected_floor):
    tmp_path, tw = rejected_floor
    # D18: only a clean whole-suite run (the default path) may drop a removal
    cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"DROP": "1"})
    assert cli("reject", "probe", "--dir", str(tw), cwd=tmp_path).returncode == 0
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "kept (rejected)" in r.stdout
    assert (tw / "baseline" / "probe.approved.json").exists()


# ---- R1-OPS-02: only a removal the run can prove is approved in bulk -----------

SUITE = {
    "conftest.py": ('import os, pytest\n'
                    '@pytest.fixture\n'
                    'def cfg():\n'
                    '    if os.environ.get("BREAK_FIXTURE"):\n'
                    '        raise FileNotFoundError("services.toml")\n'
                    '    return {"replicas": 3}\n'),
    "test_a.py": ('import os, pytest\n'
                  'def test_keep(behavior):\n'
                  '    behavior("always", 1, group="g")\n'
                  '    if not os.environ.get("DROP_ALWAYS2"):\n'
                  '        behavior("always2", 2, group="g")\n'
                  'def test_cfg(behavior, cfg):\n'
                  '    behavior("replicas", cfg["replicas"], group="g")\n'
                  '@pytest.mark.slow\n'
                  'def test_slow(behavior):\n'
                  '    behavior("slow_report", 7, group="g")\n'
                  '@pytest.mark.xfail(bool(os.environ.get("FLAKY")), reason="upstream")\n'
                  'def test_flaky(behavior):\n'
                  '    if os.environ.get("FLAKY"):\n'
                  '        raise ConnectionError("upstream down")\n'
                  '    behavior("upstream", 9, group="g")\n'),
    "test_b.py": 'def test_other(behavior):\n    behavior("other", 5, group="h")\n',
    "pytest.ini": "[pytest]\nmarkers =\n    slow: slow\n",
}


@pytest.fixture
def suite(tmp_path):
    for name, body in SUITE.items():
        write(tmp_path / name, body)
    tw = tmp_path / ".tw"
    assert cli("run", ".", "--dir", str(tw), cwd=tmp_path).returncode == 0
    assert cli("approve", "--all", "--dir", str(tw), cwd=tmp_path).returncode == 0
    return tmp_path, tw


@pytest.mark.parametrize("path, env, lost", [
    (".", {"BREAK_FIXTURE": "1"}, "replicas"),                     # setup error
    (".", {"PYTEST_ADDOPTS": '-m "not slow"'}, "slow_report"),      # deselected
    (".", {"FLAKY": "1"}, "upstream"),                             # xfail
    ("test_a.py", {}, "other"),                                     # partial path
])
def test_include_removed_holds_unproven_removals(suite, path, env, lost):
    tmp_path, tw = suite
    cli("run", path, "--dir", str(tw), cwd=tmp_path, env=env)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tw / "baseline" / f"{lost}.approved.json").exists()
    assert lost in r.stdout and "can't prove gone" in r.stdout


def test_include_removed_approves_proven_removal(suite):
    # test_keep ran to completion and no longer captures always2: a real removal.
    tmp_path, tw = suite
    cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"DROP_ALWAYS2": "1"})
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not (tw / "baseline" / "always2.approved.json").exists()


def test_narrowed_run_proves_no_removal(suite):
    # R2-OPS-02 (D13): -m/-k narrowing never proves a removal, even one whose
    # own test completed.
    tmp_path, tw = suite
    cli("run", ".", "--dir", str(tw), cwd=tmp_path,
        env={"DROP_ALWAYS2": "1", "PYTEST_ADDOPTS": '-m "not slow"'})
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert (tw / "baseline" / "always2.approved.json").exists()
    assert "narrowed" in r.stdout


def test_run_warns_about_deselected_and_xfailed(suite):
    tmp_path, tw = suite
    r = cli("run", ".", "--dir", str(tw), cwd=tmp_path,
            env={"FLAKY": "1", "PYTEST_ADDOPTS": '-m "not slow"'})
    assert "1 deselected" in r.stderr and "1 xfailed" in r.stderr
    meta = json.loads((tw / "run_meta.json").read_text(encoding="utf-8"))
    assert meta["deselected"] == 1 and meta["xfailed"] == 1


def test_source_is_recorded_but_not_compared(suite):
    tmp_path, tw = suite
    approved = json.loads((tw / "baseline" / "always.approved.json").read_text("utf-8"))
    assert approved["source"] == "test_a.py::test_keep"
    from nightward.core.behavior import Behavior
    from nightward.core.diff import compare
    (change,) = compare({"x": Behavior("x", 1, source="t.py::a")},
                        {"x": Behavior("x", 1, source="t.py::b")})
    assert change.kind == "UNCHANGED"


def test_legacy_baseline_without_source_needs_a_clean_run(suite):
    tmp_path, tw = suite
    for f in (tw / "baseline").glob("*.json"):       # baselines from before sources
        data = json.loads(f.read_text("utf-8"))
        data.pop("source", None)
        f.write_text(canonical_json(data), encoding="utf-8")
    cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"PYTEST_ADDOPTS": '-m "not slow"'})
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert (tw / "baseline" / "slow_report.approved.json").exists()
    assert "deselected" in r.stdout


# ---- R1-DATA-01: business datetimes can opt out of default scrubbing -----------

DUE = ('import os\n'
       'DUE = os.environ.get("DUE", "2024-01-05T09:00:00+00:00")\n'
       'def test_due(behavior):\n'
       '    behavior("due", {"due_utc": DUE}, group="ship", scrub=False)\n'
       'def test_seen(behavior):\n'
       '    behavior("seen", {"at": DUE}, group="ship")\n')


def test_scrub_false_lets_a_datetime_change_breach(tmp_path):
    write(tmp_path / "test_d.py", DUE)
    tw = tmp_path / ".tw"
    r = cli("run", "test_d.py", "--dir", str(tw), cwd=tmp_path)
    assert "default scrubbers masked 1 value(s) in 1 behavior(s)" in r.stderr
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    cli("run", "test_d.py", "--dir", str(tw), cwd=tmp_path,
        env={"DUE": "2031-12-25T23:59:59+09:00"})
    report = json.loads((tw / "report.json").read_text(encoding="utf-8"))
    assert [it["name"] for it in report["blast_radius"]["ship"]] == ["due"]
    assert cli("gate", "--dir", str(tw), cwd=tmp_path).returncode == 1


def test_disable_defaults_in_conftest(tmp_path):
    write(tmp_path / "conftest.py", "from nightward import scrub\nscrub.disable_defaults()\n")
    write(tmp_path / "test_d.py", DUE)
    tw = tmp_path / ".tw"
    r = cli("run", "test_d.py", "--dir", str(tw), cwd=tmp_path)
    assert "masked" not in r.stderr
    seen = json.loads((tw / "pending" / "seen.received.json").read_text(encoding="utf-8"))
    assert seen["payload"] == {"at": "2024-01-05T09:00:00+00:00"}


# ---- R1-OPS-03: a rerun attempt replaces the earlier attempt's captures ---------

def test_rerun_of_same_test_replaces_its_captures():
    rec = Recorder()
    rec.begin("t.py::a")
    rec.add("deploy.status", {"replicas": 2}, source="t.py::a")
    rec.begin("t.py::a")                                   # retry of the same test
    rec.add("deploy.status", {"replicas": 3}, source="t.py::a")
    assert [b.payload for b in rec.behaviors] == [{"replicas": 3}]
    rec.begin("t.py::b")                                   # a different test
    with pytest.raises(NightwardError, match="duplicate"):
        rec.add("deploy.status", {"replicas": 3}, source="t.py::b")


# Runs every test twice, like pytest-rerunfailures after a failed first attempt.
RERUN_CONFTEST = ('import pytest\n'
                  'from _pytest.runner import runtestprotocol\n'
                  '@pytest.hookimpl(tryfirst=True)\n'
                  'def pytest_runtest_protocol(item, nextitem):\n'
                  '    item.attempt = 1\n'
                  '    runtestprotocol(item, nextitem=nextitem, log=False)\n'
                  '    item._initrequest()  # what pytest-rerunfailures does between attempts\n'
                  '    item.attempt = 2\n')

RERUN_TEST = ('def test_deploy(behavior, request):\n'
              '    behavior("deploy.status", {"attempt": request.node.attempt}, group="d")\n')


def test_rerun_plain_pytest_passes_and_run_keeps_last_attempt(tmp_path):
    write(tmp_path / "conftest.py", RERUN_CONFTEST)
    write(tmp_path / "test_r.py", RERUN_TEST)
    plain = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                           cwd=str(tmp_path), capture_output=True, text=True)
    assert plain.returncode == 0, plain.stdout
    tw = tmp_path / ".tw"
    r = cli("run", "test_r.py", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((tw / "pending" / "deploy.status.received.json").read_text("utf-8"))
    assert got["payload"] == {"attempt": 2}
