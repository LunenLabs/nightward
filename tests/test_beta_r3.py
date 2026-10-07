"""Beta round 3: gate-integrity defects, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import os
import subprocess
import sys

import pytest


def cli(*args, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **(env or {})},
    )


def write(path, body):
    path.write_text(body, encoding="utf-8")


def baseline_names(tw):
    return sorted(f.name.split(".approved.json")[0] for f in (tw / "baseline").glob("*.json"))


# ---- R2-OPS-02 (reopened), R3-DATA-08 (D18): only a clean whole-suite run proves
# a removal -------------------------------------------------------------------

SVC_V1 = ('import pytest\n'
          'SERVICES = [{"name": "api", "replicas": 3}, {"name": "worker", "replicas": 2}]\n'
          '@pytest.mark.parametrize("svc", SERVICES)\n'
          'def test_render(behavior, svc):\n'
          '    behavior(f"svc.{svc[\'name\']}", {"replicas": svc["replicas"]}, group="r")\n')
SVC_V2 = ('import os, pytest\n'
          'NO_QUEUE = os.environ.get("NO_QUEUE") == "1"\n'
          'SERVICES = [{"name": "auth", "replicas": 1}, {"name": "api", "replicas": 3},\n'
          '            pytest.param({"name": "worker", "replicas": 2},\n'
          '                         marks=pytest.mark.skipif(NO_QUEUE, reason="no queue"))]\n'
          '@pytest.mark.parametrize("svc", SERVICES)\n'
          'def test_render(behavior, svc):\n'
          '    behavior(f"svc.{svc[\'name\']}", {"replicas": svc["replicas"]}, group="r")\n')


def test_shifted_parametrize_id_is_no_removal_proof(tmp_path):
    write(tmp_path / "test_svc.py", SVC_V1)
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_svc.py", SVC_V2)
    cli("run", ".", "--dir", str(tw), cwd=tmp_path, env={"NO_QUEUE": "1"})
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "svc.worker" in baseline_names(tw)
    assert "1 skipped" in r.stdout


def test_capture_moved_into_a_skipped_test_in_one_change_is_kept(tmp_path):
    write(tmp_path / "test_a.py",
          'def test_one(behavior):\n'
          '    behavior("render.dev", "dev", group="r")\n'
          '    behavior("render.meta", 1, group="r")\n')
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_a.py",
          'import pytest\n'
          'def test_one(behavior):\n'
          '    behavior("render.meta", 1, group="r")\n'
          '@pytest.mark.skip(reason="no schema")\n'
          'def test_dev(behavior):\n'
          '    behavior("render.dev", "dev", group="r")\n')
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert "render.dev" in baseline_names(tw), r.stdout


@pytest.fixture
def legacy_pair(tmp_path):
    """Baselines from before `source` existed (R3-DATA-08)."""
    tw = tmp_path / ".nightward"
    (tw / "baseline").mkdir(parents=True)
    for name, value in (("a", 1), ("b", 2)):
        write(tw / "baseline" / f"{name}.approved.json",
              json.dumps({"group": "g", "name": name, "payload": value}, indent=2) + "\n")
        write(tmp_path / f"test_{name}.py",
              f'def test_{name}(behavior):\n    behavior("{name}", {value}, group="g")\n')
    return tmp_path, tw


@pytest.mark.parametrize("pytest_args, env", [
    (["--collect-only"], {}),
    (["--setup-plan"], {}),
    (["--ignore=test_b.py"], {}),
    ([], {"PYTEST_ADDOPTS": "--ignore=test_b.py"}),
])
def test_run_that_did_not_run_everything_proves_no_removal(legacy_pair, pytest_args, env):
    tmp_path, tw = legacy_pair
    cli("run", ".", "--", *pytest_args, cwd=tmp_path, env=env)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert baseline_names(tw) == ["a", "b"], r.stdout
    assert "clean whole-suite run" in r.stdout


def test_direct_collect_only_capture_proves_no_removal(legacy_pair):
    tmp_path, tw = legacy_pair
    subprocess.run([sys.executable, "-m", "pytest", "--co", "-q", "--nightward-record",
                    "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True)
    cli("report", cwd=tmp_path)
    cli("review", cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert baseline_names(tw) == ["a", "b"], r.stdout + r.stderr


def test_clean_whole_suite_run_still_proves_a_legacy_removal(legacy_pair):
    tmp_path, tw = legacy_pair
    write(tmp_path / "test_b.py", "def test_b():\n    pass\n")
    cli("run", ".", cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert baseline_names(tw) == ["a"], r.stdout


def test_renamed_source_test_is_no_removal_proof(tmp_path):
    write(tmp_path / "test_a.py", 'def test_a(behavior):\n    behavior("a", 1)\n'
                                  'def test_b(behavior):\n    behavior("b", 2)\n')
    tw = tmp_path / ".tw"
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    cli("approve", "--all", "--dir", str(tw), cwd=tmp_path)
    write(tmp_path / "test_a.py", 'def test_a(behavior):\n    behavior("a", 1)\n'
                                  'def test_b2():\n    pass\n')
    cli("run", ".", "--dir", str(tw), cwd=tmp_path)
    r = cli("approve", "--all", "--include-removed", "--dir", str(tw), cwd=tmp_path)
    assert "b" in baseline_names(tw)
    assert "test_a.py::test_b did not run" in r.stdout


# ---- D19: human decisions bind to exactly what the human saw -------------------

SHOP = ('import os\n'
        'UNIT = int(os.environ.get("UNIT", "10"))\n'
        'LABEL = os.environ.get("LABEL", "Total")\n'
        'def checkout(qty):\n    return {"qty": qty, "total": qty * UNIT}\n'
        'def banner():\n    return {"label": LABEL}\n')
TEST_SHOP = ('from shop import checkout, banner\n'
             'def test_checkout(behavior):\n'
             '    behavior("checkout.3", checkout(3), group="billing")\n'
             'def test_banner(behavior):\n    behavior("ui.banner", banner(), group="ui")\n')


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """Approved shop; then an agent run changed the price (regression) and the label."""
    write(tmp_path / "shop.py", SHOP)
    write(tmp_path / "test_shop.py", TEST_SHOP)
    assert cli("init", cwd=tmp_path).returncode == 0
    cli("run", ".", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNIT", "12")
    monkeypatch.setenv("LABEL", "Order total")
    from nightward.mcp_server import run_tool
    assert run_tool(".")["boundary"] == "breached"      # agent: never a review
    monkeypatch.delenv("UNIT")
    monkeypatch.delenv("LABEL")
    return tmp_path, tmp_path / ".nightward"


def test_scoped_review_approves_only_what_it_showed(shop):
    # R3-WEB-01: `review --group ui` showed only the banner.
    tmp_path, tw = shop
    r = cli("review", "--group", "ui", cwd=tmp_path)
    assert "Order total" in r.stdout and "[CHANGED] checkout.3" not in r.stdout
    assert "outside this selection, not reviewed: checkout.3" in r.stdout
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 2 and "checkout.3" in r.stderr, r.stdout
    r = cli("approve", "checkout.3", cwd=tmp_path)
    assert r.returncode == 2 and "nightward review checkout.3" in r.stderr
    base = json.loads((tw / "baseline" / "checkout.3.approved.json").read_text("utf-8"))
    assert base["payload"]["total"] == 30
    assert cli("approve", "ui.banner", cwd=tmp_path).returncode == 0
    # a second scoped review adds to what was seen
    cli("review", "checkout.3", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    assert cli("gate", cwd=tmp_path).returncode == 0


def test_reject_records_the_reviewed_capture_or_refuses(shop, monkeypatch):
    # R3-FIN-02: an agent run between review and reject.
    tmp_path, tw = shop
    cli("review", cwd=tmp_path)
    monkeypatch.setenv("UNIT", "11")
    from nightward.mcp_server import run_tool
    run_tool(".")
    r = cli("reject", "checkout.3", cwd=tmp_path)
    assert r.returncode == 2 and "changed since" in r.stderr, r.stdout
    assert not (tw / "rejected").exists() or not list((tw / "rejected").iterdir())


def test_reject_refuses_a_name_that_is_not_a_change(shop):
    # R3-FIN-07: a one-word slip must not "reject" an unchanged behavior.
    tmp_path, tw = shop
    fee = 'def test_fee(behavior):\n    behavior("checkout.fee", 0, group="billing")\n'
    write(tmp_path / "test_shop.py", TEST_SHOP + fee)
    cli("run", ".", cwd=tmp_path, env={"UNIT": "12"})
    assert cli("approve", "checkout.fee", cwd=tmp_path).returncode == 0
    cli("review", cwd=tmp_path)
    r = cli("reject", "checkout.fee", cwd=tmp_path)
    assert r.returncode == 2, r.stdout
    assert "checkout.fee" in r.stderr and "checkout.3" in r.stderr
    assert not (tw / "rejected" / "checkout.fee.rejected.json").exists()


def test_approve_refuses_a_stale_report(shop):
    # R3-OPS-03: a `git pull` brought a teammate's baseline after the review.
    tmp_path, tw = shop
    cli("review", cwd=tmp_path)
    f = tw / "baseline" / "checkout.3.approved.json"
    data = json.loads(f.read_text("utf-8"))
    data["payload"]["total"] = 33
    f.write_text(json.dumps(data), encoding="utf-8")
    r = cli("approve", "--all", cwd=tmp_path)
    assert r.returncode == 2 and "nightward run" in r.stderr, r.stdout
    assert json.loads(f.read_text("utf-8"))["payload"]["total"] == 33


LOAN = ('import os\n'
        'def test_loan_email(behavior):\n'
        '    d = os.environ.get("DECISION", "approved")\n'
        '    behavior("loan.email", f"Your loan application was {d}.", group="email",'
        ' semantic=True)\n')


def test_reject_overrules_a_judged_same(tmp_path):
    # R3-WEB-03: a human rejection beats the judge's SAME.
    write(tmp_path / "pyproject.toml", '[tool.nightward]\njudge = "persona:lenient"\n')
    write(tmp_path / "test_loan.py", LOAN)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("approve", "--all", cwd=tmp_path)
    r = cli("run", ".", cwd=tmp_path, env={"DECISION": "denied"})
    assert r.returncode == 0 and "intact" in r.stdout          # lenient: SAME
    assert cli("review", cwd=tmp_path).returncode == 0
    r = cli("reject", "loan.email", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert cli("gate", cwd=tmp_path).returncode == 1
    cli("run", ".", cwd=tmp_path, env={"DECISION": "denied"})
    assert cli("gate", cwd=tmp_path).returncode == 1
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert status["boundary"] == "breached"
    assert status["changes"][0]["name"] == "loan.email" and status["changes"][0]["rejected"]


def test_standing_rejection_is_visible_everywhere(shop):
    # R3-FIN-03: a teammate's committed rejection of exactly this payload.
    tmp_path, tw = shop
    cli("review", cwd=tmp_path)
    assert cli("reject", "checkout.3", cwd=tmp_path).returncode == 0
    for args in (("run", "."), ("review",), ("status",)):
        out = cli(*args, cwd=tmp_path, env={"UNIT": "12", "LABEL": "Order total"}).stdout
        assert "rejected" in out, (args, out)
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert [c["name"] for c in status["changes"] if c.get("rejected")] == ["checkout.3"]
    cli("view", "--no-serve", cwd=tmp_path)
    data = json.loads((tmp_path / "nightward-site" / "data.json").read_text("utf-8"))
    items = [it for its in data["report"]["blast_radius"].values() for it in its]
    assert [it["name"] for it in items if it.get("rejected")] == ["checkout.3"]
    r = cli("approve", "checkout.3", cwd=tmp_path)
    assert r.returncode == 0 and "overrides" in r.stdout


def test_baseline_equal_to_a_rejection_breaches(shop):
    # R3-FIN-03 variant: a merge brought an approval and a rejection of one payload.
    tmp_path, tw = shop
    cli("review", cwd=tmp_path)
    assert cli("reject", "checkout.3", cwd=tmp_path).returncode == 0
    rec = (tw / "rejected" / "checkout.3.rejected.json").read_text("utf-8")
    cli("approve", "checkout.3", cwd=tmp_path)              # the other branch
    (tw / "rejected").mkdir(exist_ok=True)
    (tw / "rejected" / "checkout.3.rejected.json").write_text(rec, encoding="utf-8")
    cli("run", ".", cwd=tmp_path, env={"UNIT": "12", "LABEL": "Order total"})
    assert cli("gate", cwd=tmp_path).returncode == 1
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert any(c["name"] == "checkout.3" and c.get("rejected") for c in status["changes"])


PAYOUT = ('import os\n'
          'def test_payout(behavior):\n'
          '    fee = int(os.environ.get("FEE_BPS", "290"))\n'
          '    behavior("payout.net", {"net": 10000 - 10000 * fee // 10000}, group="p")\n')


@pytest.fixture
def payout(tmp_path):
    write(tmp_path / "test_payout.py", PAYOUT)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("approve", "--all", cwd=tmp_path)
    assert cli("gate", cwd=tmp_path).returncode == 0
    return tmp_path, tmp_path / ".nightward"


def test_run_refused_by_a_bad_judge_config_invalidates_the_report(payout, monkeypatch):
    # R3-FIN-01: refused before pytest started - the old "intact" must not stand.
    tmp_path, tw = payout
    write(tmp_path / "pyproject.toml", '[tool.nightward]\njudge = "persona:edtor"\n')
    r = cli("run", ".", cwd=tmp_path, env={"FEE_BPS": "390"})
    assert r.returncode == 2 and "edtor" in r.stderr
    assert "$ pytest" not in r.stdout
    assert cli("gate", cwd=tmp_path).returncode != 0
    cli("run", ".", cwd=tmp_path)            # still refused; and via MCP:
    monkeypatch.chdir(tmp_path)
    from nightward import mcp_server
    write(tw / "report.json", "{}")
    with pytest.raises(Exception, match="edtor"):
        mcp_server.run_tool(".")
    assert mcp_server.status_tool()["boundary"] == "unknown"


def test_run_refused_by_a_busy_lock_invalidates_the_report(payout):
    tmp_path, tw = payout
    write(tw / ".lock", json.dumps({"pid": os.getpid(), "host": __import__("socket").gethostname(),
                                    "command": "nightward_run (MCP)", "since": "now"}))
    r = cli("run", ".", cwd=tmp_path, env={"FEE_BPS": "390"})
    assert r.returncode == 2 and "another nightward process" in r.stderr
    assert cli("gate", cwd=tmp_path).returncode != 0


def test_run_echo_shows_the_passthrough_args(payout):
    # R3-DATA-03: the logged command is how CI readers check what ran.
    tmp_path, tw = payout
    r = cli("run", ".", "--", "-m", "not slow", "-p", "no:randomly", cwd=tmp_path)
    assert "$ pytest . -m 'not slow' -p no:randomly --nightward-record" in r.stdout, r.stdout


# ---- R3-DATA-02 (D21): explicit deselection is "not checked", not "removed" ------

ML = ('import os, pytest\n'
      'def test_features(behavior):\n'
      '    behavior("feature_stats", {"rows": 1000}, group="features")\n'
      '@pytest.mark.gpu\n'
      'def test_train(behavior):\n'
      '    behavior("model_metrics", {"auc": 0.912}, group="model")\n'
      '@pytest.mark.skipif(bool(os.environ.get("NO_EVAL")), reason="no eval data")\n'
      'def test_eval(behavior):\n'
      '    behavior("eval_metrics", {"f1": 0.8}, group="model")\n')


@pytest.fixture
def ml(tmp_path):
    write(tmp_path / "pytest.ini", "[pytest]\nmarkers =\n    gpu: needs a GPU\n")
    write(tmp_path / "test_ml.py", ML)
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    assert cli("approve", "--all", cwd=tmp_path).returncode == 0
    return tmp_path, tmp_path / ".nightward"


def test_deselected_behaviors_are_not_checked_not_removed(ml):
    tmp_path, tw = ml
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-m", "not gpu", "--nightward-record",
                    "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True)
    r = cli("report", cwd=tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REMOVED" not in r.stdout
    assert "1 behavior(s) not checked" in r.stdout and "model_metrics" in r.stdout
    assert cli("gate", cwd=tmp_path).returncode == 1          # D23: partial, not done
    assert cli("gate", "--allow-not-run", cwd=tmp_path).returncode == 0
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert status["boundary"] == "partial" and status["narrowed"] is True
    assert [n["name"] for n in status["not_run"]] == ["model_metrics"]
    # never removal proof, and nothing to approve by name
    r = cli("approve", "--all", "--include-removed", cwd=tmp_path)
    assert "model_metrics" in baseline_names(tw)
    r = cli("approve", "model_metrics", cwd=tmp_path)
    assert r.returncode == 2 and "not checked" in r.stderr
    assert "model_metrics" in baseline_names(tw)


def test_run_passthrough_deselection_is_not_checked(ml):
    tmp_path, tw = ml
    r = cli("run", ".", "--", "-m", "not gpu", cwd=tmp_path)
    assert r.returncode == 0 and "not checked" in r.stdout, r.stdout + r.stderr
    assert cli("gate", "--allow-not-run", cwd=tmp_path).returncode == 0   # D23 opt-in


def test_skipped_capture_stays_removed(ml):
    # Skips stay fail-closed: the test was selected and did not capture.
    tmp_path, tw = ml
    cli("run", ".", cwd=tmp_path, env={"NO_EVAL": "1"})
    assert cli("gate", cwd=tmp_path).returncode == 1
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert [c["name"] for c in status["changes"] if c["kind"] == "REMOVED"] == ["eval_metrics"]


# ---- R3-WEB-02: names arrive literally (no Click expansion on Windows) ----------

def test_quoted_names_are_never_expanded(tmp_path):
    write(tmp_path / "test_p.py",
          'def test_prices(behavior):\n'
          '    behavior("price.$region", {"total": 30}, group="billing")\n'
          '    behavior("price.eu", {"total": 99}, group="billing")\n'
          '    behavior("p.%OS%", 1, group="billing")\n'
          '    behavior("~home", 2, group="billing")\n')
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    env = {"region": "eu", "OS": "Windows_NT", "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)}
    for name in ("price.$region", "p.%OS%", "~home"):
        r = cli("approve", name, cwd=tmp_path, env=env)
        assert r.returncode == 0 and f"approved {name} (" in r.stdout, (name, r.stdout, r.stderr)
    assert baseline_names(tmp_path / ".nightward") == ["p.%OS%", "price.$region", "~home"]


# ---- R3-OPS-02: the MCP server in a subdirectory never makes a second store ------

def test_mcp_in_a_subdirectory_refuses_to_create_a_second_store(tmp_path, monkeypatch):
    from nightward import mcp_server
    from nightward.errors import NightwardError
    (tmp_path / "tests").mkdir()
    write(tmp_path / "tests" / "test_a.py", 'def test_a(behavior):\n    behavior("a", 1)\n')
    cli("init", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path)
    cli("approve", "--all", cwd=tmp_path)
    monkeypatch.chdir(tmp_path / "tests")
    for call in (mcp_server.status_tool, mcp_server.run_tool):
        with pytest.raises(NightwardError, match="start the MCP server in the project root"):
            call()
    assert not (tmp_path / "tests" / ".nightward").exists()


# ---- R3-OPS-01: releasing the lock survives a reader holding the file -----------

def test_lock_release_survives_a_reader_holding_the_file(tmp_path):
    from nightward.core.lock import read_lock, store_lock
    with store_lock(tmp_path, "nightward approve"):        # release must not raise
        reader = open(tmp_path / ".lock", encoding="utf-8")   # a contender reading it
        reader.seek(0)
    try:
        # Windows can't delete it yet: whatever is left must not name a live holder
        left = read_lock(tmp_path)
        assert left is None or left.get("released") is True
    finally:
        reader.close()
    with store_lock(tmp_path, "nightward run"):           # the next writer takes it
        pass
    assert not (tmp_path / ".lock").exists()


def test_lock_release_retries_a_brief_sharing_violation(tmp_path):
    import threading

    from nightward.core.lock import store_lock
    with store_lock(tmp_path, "nightward reject"):
        reader = open(tmp_path / ".lock", encoding="utf-8")
        threading.Timer(0.1, reader.close).start()
    assert not (tmp_path / ".lock").exists()


# ---- R3-FIN-05: a bare `pytest --nightward-record` checks the lock up front -----

def test_direct_record_on_a_busy_store_fails_before_the_suite(tmp_path):
    from nightward.core.lock import store_lock
    write(tmp_path / "test_slow.py",
          'from pathlib import Path\n'
          'def test_slow(behavior):\n'
          '    Path("ran.txt").write_text("x")\n'
          '    behavior("ledger.balance", 1)\n')
    tw = tmp_path / ".nightward"
    with store_lock(tw, "nightward run"):
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "--nightward-record",
                            "-p", "no:cacheprovider"], cwd=tmp_path, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    out = r.stdout + r.stderr
    assert r.returncode == 4, out
    assert "another nightward process" in out and "Traceback" not in out
    assert not (tmp_path / "ran.txt").exists()


# ---- R3-FIN-06, R3-OPS-04: outdated or overbroad ignore rules are named ---------

OLD_GITIGNORE = ("# nightward: approved baseline IS committed; transient state is not\n"
                 ".nightward/pending/\n.nightward/rejected/\n.nightward/report.json\n"
                 ".nightward/run_meta.json\n.nightward/pending.tmp/\n.nightward/**/*.tmp\n")


def _git_project(tmp_path, gitignore):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write(tmp_path / ".gitignore", gitignore)
    write(tmp_path / "test_fee.py", 'import os\ndef test_fee(behavior):\n'
                                    '    behavior("fee", os.environ.get("FEE", "2.9"))\n')
    Path_ = __import__("pathlib").Path
    Path_(tmp_path / ".nightward").mkdir()


def test_run_names_an_outdated_gitignore(tmp_path):
    _git_project(tmp_path, OLD_GITIGNORE)
    r = cli("run", ".", cwd=tmp_path)
    assert "rejections (.nightward/rejected/) are git-ignored" in r.stderr, r.stderr
    assert "reviewed.json" in r.stderr and ".lock" in r.stderr
    cli("approve", "--all", cwd=tmp_path)
    cli("run", ".", cwd=tmp_path, env={"FEE": "3.9"})
    r = cli("reject", "fee", cwd=tmp_path)
    assert r.returncode == 0 and "rejections" in r.stderr and "commit" not in r.stdout
    # init migrates the lines it owns; then run is quiet
    assert cli("init", cwd=tmp_path).returncode == 0
    r = cli("run", ".", cwd=tmp_path, env={"FEE": "3.9"})
    assert "warning" not in r.stderr, r.stderr


def test_init_names_a_rule_that_ignores_the_baseline(tmp_path):
    _git_project(tmp_path, ".nightward/\n")
    r = cli("init", cwd=tmp_path)
    assert r.returncode == 0
    assert "store exists" in r.stdout
    assert "approved baseline" in r.stderr and ".gitignore:1:.nightward/" in r.stderr, r.stderr
    r = cli("run", ".", cwd=tmp_path)
    assert "approved baseline" in r.stderr


def test_busy_lock_from_another_host_says_it_was_committed(tmp_path):
    tw = tmp_path / ".nightward"
    tw.mkdir()
    write(tw / ".lock", json.dumps({"pid": 1, "host": "dev-laptop-anna",
                                    "command": "nightward run", "since": "then"}))
    write(tmp_path / "test_a.py", 'def test_a(behavior):\n    behavior("a", 1)\n')
    r = cli("run", ".", cwd=tmp_path)
    assert r.returncode == 2 and "dev-laptop-anna" in r.stderr
    assert "git rm --cached" in r.stderr


# ---- R3-DATA-07: an unchanged capture reuses its files instead of rewriting them --

def test_replace_pending_reuses_unchanged_files(tmp_path):
    from nightward.core.baseline import Store
    from nightward.core.behavior import Behavior
    store = Store(tmp_path / ".nightward")
    store.replace_pending([Behavior("a", 1, group="g"), Behavior("b", 2, group="g")])
    ino = {n: (store.pending_dir / f"{n}.received.json").stat().st_ino for n in "ab"}
    store.replace_pending([Behavior("a", 1, group="g"), Behavior("b", 3, group="g")])
    assert (store.pending_dir / "a.received.json").stat().st_ino == ino["a"]
    assert store.load_pending()["b"].payload == 3
    assert not (store.root / "pending.tmp").exists()
