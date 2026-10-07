"""Beta round 3: dashboard/CLI UX defects reported by beta testers, frozen as tests.

Each test failed on the code before its fix. A failure here is a real defect -
fix the code, do not weaken the test.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute
from nightward.shellquote import SHELLS, quote
from nightward.view import collect_data

APP_JS = Path(__file__).parents[1] / "src" / "nightward" / "view" / "assets" / "app.js"


def cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def node_eval(js_expr):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    runner = (
        "const vm = require('vm'), fs = require('fs');"
        "const ctx = {fetch: () => new Promise(() => {}), console};"
        "vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);"
        "process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2], ctx)));"
    )
    r = subprocess.run([node, "-e", runner, str(APP_JS), js_expr], capture_output=True,
                       text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ---- R3-DATA-06: `approve --group G` approves a group of any size --------------

def partition_store(tw, n=300):
    """`n` CHANGED partitions in group partitions.kr, a CHANGED job.totals (a
    regression) in group summary, and part.gone REMOVED (its test didn't run)."""
    store = Store(tw)
    store.ensure()
    parts = [f"part.dt-{i:04d}.KR" for i in range(n)]
    for name in [*parts, "part.gone"]:
        store.write_pending(Behavior(name=name, payload=1, group="partitions.kr",
                                     source=f"t.py::{'gone' if name == 'part.gone' else 'h'}"))
        store.approve(name)
    store.write_pending(Behavior(name="job.totals", payload=1440, group="summary",
                                 source="t.py::h"))
    store.approve("job.totals")
    store.clear_pending()
    for name in parts:
        store.write_pending(Behavior(name=name, payload=2, group="partitions.kr",
                                     source="t.py::h"))
    store.write_pending(Behavior(name="job.totals", payload=1439, group="summary",
                                 source="t.py::h"))
    store.write_run_meta({"completed": ["t.py::h"]})
    recompute(store)
    assert cli("review", "--dir", str(tw), cwd=tw.parent).returncode == 0
    return store, parts


def test_approve_group_promotes_every_name_in_that_group_only(tmp_path):
    store, parts = partition_store(tmp_path / ".tw")
    r = cli("approve", "--group", "partitions.kr", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    base = store.load_baseline()
    assert all(base[n].payload == 2 for n in parts)
    assert base["job.totals"].payload == 1440       # the other group's regression stays
    report = store.load_report()
    assert report["boundary"] == "breached"
    assert {it["name"] for items in report["blast_radius"].values() for it in items} == {
        "job.totals", "part.gone"}


def test_approve_group_keeps_removals_and_rejections_like_all(tmp_path):
    store, parts = partition_store(tmp_path / ".tw", n=3)
    assert cli("reject", parts[0], "--dir", str(store.root), cwd=tmp_path).returncode == 0
    r = cli("approve", "--group", "partitions.kr", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    base = store.load_baseline()
    assert base[parts[0]].payload == 1 and "kept (rejected)" in r.stdout
    assert base[parts[1]].payload == 2
    assert "part.gone" in base and "kept 1 REMOVED" in r.stdout
    assert "--group ... --include-removed" in r.stdout


def test_approve_group_include_removed_still_needs_proof(tmp_path):
    store, _ = partition_store(tmp_path / ".tw", n=3)
    r = cli("approve", "--group", "partitions.kr", "--include-removed", "--dir",
            str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "part.gone" in store.load_baseline() and "can't prove gone" in r.stdout


def test_approve_group_with_one_member_is_still_a_bulk_approval(tmp_path):
    # One explicit name overrides proof; a group never does, whatever its size.
    store = Store(tmp_path / ".tw")
    store.ensure()
    store.write_pending(Behavior(name="solo", payload=1, group="g", source="t.py::gone"))
    store.approve("solo")
    store.clear_pending()
    store.write_run_meta({"completed": []})
    recompute(store)
    assert cli("review", "--dir", str(store.root), cwd=tmp_path).returncode == 0
    r = cli("approve", "--group", "g", "--include-removed", "--dir", str(store.root),
            cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "solo" in store.load_baseline() and "can't prove gone" in r.stdout


def test_approve_group_with_nothing_unapproved_says_so(tmp_path):
    store, _ = partition_store(tmp_path / ".tw", n=2)
    assert cli("approve", "--group", "summary", "--dir", str(store.root),
               cwd=tmp_path).returncode == 0
    r = cli("approve", "--group", "summary", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "nothing to approve in that group" in r.stdout


def test_approve_unknown_group_is_an_error_not_nothing_to_do(tmp_path):
    store, _ = partition_store(tmp_path / ".tw", n=2)
    r = cli("approve", "--group", "partitions.kx", "--dir", str(store.root), cwd=tmp_path)
    assert r.returncode == 2
    assert "partitions.kx" in r.stderr and "Traceback" not in r.stderr


def test_approve_group_with_names_or_all_is_an_error(tmp_path):
    store, parts = partition_store(tmp_path / ".tw", n=2)
    for extra in (["--all"], [parts[0]]):
        r = cli("approve", "--group", "partitions.kr", *extra, "--dir", str(store.root),
                cwd=tmp_path)
        assert r.returncode == 2, extra
    assert store.load_baseline()[parts[0]].payload == 1


def test_dashboard_group_chip_is_one_short_command():
    quoted = {"partitions.kr": {s: quote("partitions.kr", s) for s in SHELLS},
              "a b": {s: quote("a b", s) for s in SHELLS}}
    got = node_eval(f"setQuoting({json.dumps(quoted)}, 'posix'); "
                    f"[groupApproveCommand('partitions.kr'), groupApproveCommand('a b')]")
    assert got == ["nightward approve --group partitions.kr", "nightward approve --group 'a b'"]


def test_dashboard_data_quotes_group_names(tmp_path):
    store, _ = partition_store(tmp_path / ".tw", n=2)
    quoted = collect_data(store.root)["quoted"]
    assert quoted["partitions.kr"]["posix"] == "partitions.kr"
    assert "summary" in quoted


# ---- R3-WEB-04: the dashboard after an aborted or invalidated run --------------

def aborted_store(tw):
    """An approved baseline whose last run aborted: the report was invalidated."""
    store = Store(tw)
    store.ensure()
    store.write_pending(Behavior(name="a", payload=1))
    store.approve("a")
    recompute(store)
    store.invalidate_report()
    return store


def test_dashboard_without_a_report_shows_an_unknown_verdict(tmp_path):
    data = collect_data(aborted_store(tmp_path / ".nightward").root)
    assert data["report"] is None
    got = node_eval(f"[bannerState(null, {json.dumps(data['meta'])}), "
                    f"noReportState({json.dumps(data['meta'])})]")
    state, empty = got
    assert state == "unknown"
    assert "did not produce a verdict" in empty["body"]
    assert "nightward view" in empty["body"]
    assert "refresh this page" not in empty["body"]


def test_dashboard_with_nothing_captured_says_so(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    meta = collect_data(store.root)["meta"]
    empty = node_eval(f"noReportState({json.dumps(meta)})")
    assert empty["title"] == "No run recorded yet"
    assert "nightward view" in empty["body"]


def test_dashboard_run_command_is_this_stores_not_the_quickstart(tmp_path):
    store = aborted_store(tmp_path / "custom store")
    meta = collect_data(store.root)["meta"]
    assert meta["run_command"]["posix"] == f"nightward run . --dir '{store.root}'"
    assert meta["run_command"]["cmd"] == f'nightward run . --dir "{store.root}"'
    js = APP_JS.read_text(encoding="utf-8")
    assert "nightward run example" not in js
    assert "refresh this page" not in js


def test_dashboard_run_command_has_no_dir_for_the_default_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    aborted_store(Path(".nightward"))
    meta = collect_data(".nightward")["meta"]
    assert meta["run_command"] == {s: "nightward run ." for s in SHELLS}
    assert node_eval(f"setQuoting({{}}, 'posix'); runCommand({json.dumps(meta)})") == (
        "nightward run .")


def test_dashboard_dates_the_verdict_with_its_offset(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    report = recompute(store)
    meta = collect_data(store.root)["meta"]
    # the build time carries its UTC offset, like the verdict's own time
    assert meta["generated"][-6] in "+-" and meta["generated"][-3] == ":"
    assert node_eval(f"metaItems({json.dumps(report)}, {json.dumps(meta)})")[0]["text"] == (
        "verdict as of: " + report["generated_at"])


def test_dashboard_counts_judged_same_inside_unchanged():
    labels = node_eval("countItems({unchanged: 10, changed: 0, new: 0, removed: 0, "
                       "judged_same: 1}).map(function (i) { return i[1]; })")
    assert labels[-1] == "of them judged same"
    assert not any("(AI)" in label for label in labels)


@pytest.mark.parametrize("model, word", [("persona:editor", "rule-judged"),
                                         ("anthropic:claude-haiku-4-5", "AI-judged")])
def test_dashboard_does_not_call_a_persona_an_ai(model, word):
    assert node_eval(f"judgeBadge({json.dumps(model)})") == word


# ---- R3-WEB-05: the --no-serve hint keeps the loopback-only guarantee ----------

def test_no_serve_hint_binds_loopback(tmp_path):
    Store(tmp_path / ".nightward").ensure()
    r = cli("view", "--no-serve", "--out", "my site", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout.replace("\n", " ")
    assert "http.server" in out and "--bind 127.0.0.1" in out, out
    # a path with a space arrives as one argument
    assert "'my site'" in out or '"my site"' in out, out
    assert "nightward view --out" in out


# ---- R1-WEB-06 retest: advice that can actually silence the warning -----------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@needs_git
def test_view_with_a_custom_out_dir_suggests_its_own_ignore_line(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert cli("init", cwd=tmp_path).returncode == 0
    r = cli("view", "--no-serve", "--out", "blast-radius", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    err = r.stderr.replace("\n", " ")
    assert "not git-ignored" in err and "blast-radius/" in err, err
    assert "nightward init" not in err, err


def test_init_on_an_existing_store_does_not_claim_it_created_one(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    for n in ("a", "b"):
        store.write_pending(Behavior(name=n, payload=1))
        store.approve(n)
    r = cli("init", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "created" not in r.stdout and "2 approved" in r.stdout, r.stdout
    assert "approve --all" not in r.stdout, r.stdout
