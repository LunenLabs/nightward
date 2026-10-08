"""Beta round 3 (AI/judge/doctor senior): the ledger is a reviewable record, not
an oracle for deterministic personas (D22), the judge comes from the store's
project, scrub rules are scoped to their conftest (D20), and doctor's advice
equalizes the noise it diagnoses."""
import json
import os
import subprocess
import sys

import pytest

from nightward.errors import NightwardError
from nightward.judge import DIFFERENT, SAME, Judge

REPLY_TEST = '''
import os
def test_reply(behavior):
    text = os.environ.get("REPLY", "Your refund of $250.00 needs a manager's review.")
    behavior("reply.refund", text, group="chat", semantic=True)
'''
REGRESSED = "Your refund of $250.00 has been approved."
REWORDED = "your refund of $250.00 needs a manager's review"


def cli(*args, cwd, env=None):
    return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=str(cwd),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("NIGHTWARD_JUDGE", "REPLY")}
    return env | extra


def _pyproject(folder, judge=None, extra=""):
    table = f'[tool.nightward]\njudge = "{judge}"\n' if judge else ""
    (folder / "pyproject.toml").write_text(table + extra, encoding="utf-8")


def _approved(tmp_path, judge="persona:editor"):
    (tmp_path / "test_reply.py").write_text(REPLY_TEST, encoding="utf-8")
    if judge:
        _pyproject(tmp_path, judge)
    assert cli("run", ".", cwd=tmp_path, env=_env()).returncode == 0
    cli("review", cwd=tmp_path)   # D26: only review marks
    assert cli("approve", "--all", cwd=tmp_path, env=_env()).returncode == 0


def _ledger_entries(store_root):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((store_root / "judge").glob("*.json"))]


# --- R3-LLM-02 (D22): a persona ruling is recomputed, never replayed ----------


def test_hand_edited_persona_ruling_does_not_decide_the_verdict(tmp_path):
    _approved(tmp_path)
    r = cli("run", ".", cwd=tmp_path, env=_env(REPLY=REGRESSED))
    assert "breached" in r.stdout
    [path] = (tmp_path / ".nightward" / "judge").glob("*.json")
    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["verdict"] == DIFFERENT
    entry |= {"verdict": SAME, "reason": "only case differs"}       # the agent's edit
    path.write_text(json.dumps(entry), encoding="utf-8")

    r = cli("run", ".", cwd=tmp_path, env=_env(REPLY=REGRESSED))
    assert "breached" in r.stdout, r.stdout
    assert "did not match" in r.stderr and "reply.refund" in r.stderr
    assert cli("gate", cwd=tmp_path).returncode == 1
    # the ledger is rewritten with the persona's own ruling (visible in git diff)
    assert json.loads(path.read_text(encoding="utf-8"))["verdict"] == DIFFERENT


def test_persona_judge_rejudges_even_with_a_matching_ledger_entry(tmp_path, monkeypatch):
    import nightward.judge as judge_mod
    calls = []

    def counting(model, old, new):
        calls.append(model)
        return SAME, "counted"

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", counting)
    for _ in range(2):
        v = Judge("persona:editor", cache_path=tmp_path / "c.json").equivalent(
            "a", "b", "f1", "f2", name="x")
        assert v.verdict == SAME and not v.cached
    assert len(calls) == 2


def test_api_judge_ruling_is_replayed_and_marked_as_replayed(tmp_path, monkeypatch):
    import nightward.judge as judge_mod
    from nightward.core.behavior import Behavior
    from nightward.core.diff import compare
    from nightward.judge import JudgeUnavailable
    spec = "anthropic:claude-haiku-4-5"
    monkeypatch.setitem(judge_mod._BACKENDS, "anthropic", lambda m, o, n: (SAME, "rephrased"))
    base = {"p": Behavior("p", "old wording", semantic=True)}
    pend = {"p": Behavior("p", "new wording", semantic=True)}
    [fresh] = compare(base, pend, judge=Judge(spec, cache_path=tmp_path / "c.json"))
    assert fresh.judged and not fresh.to_dict().get("judge_replayed")

    def down(model, old, new):
        raise JudgeUnavailable("no key on CI")

    monkeypatch.setitem(judge_mod._BACKENDS, "anthropic", down)
    [replayed] = compare(base, pend, judge=Judge(spec, cache_path=tmp_path / "c.json"))
    assert replayed.kind == "UNCHANGED" and replayed.to_dict()["judge_replayed"] is True


def test_status_says_a_ruling_was_replayed_from_the_ledger():
    from nightward.signal import status_payload
    report = {"boundary": "intact", "unapproved": 0, "counts": {}, "blast_radius": {},
              "judged_same": [{"name": "p", "group": "g", "kind": "UNCHANGED",
                               "judged": True, "judge_model": "anthropic:x",
                               "judge_reason": "r", "judge_replayed": True, "diff": ""}]}
    assert status_payload(report)["judged_same"][0]["judge_replayed"] is True


def test_run_lists_the_judged_same_behaviors(tmp_path):
    _approved(tmp_path)
    r = cli("run", ".", cwd=tmp_path, env=_env(REPLY=REWORDED))
    assert "intact" in r.stdout
    assert "ruled semantically SAME" in r.stdout and "reply.refund" in r.stdout
    s = cli("status", cwd=tmp_path)
    assert "reply.refund" in s.stdout


# --- R3-FIN-04 (D22): one file per ruling; a conflict is named as one ----------


def test_rulings_are_stored_one_file_per_ruling_with_a_trailing_newline(tmp_path):
    j = Judge("persona:editor", cache_path=tmp_path / "judge_verdicts.json")
    j.equivalent("Hello there.", "hello there", "a" * 64, "b" * 64, name="one")
    j.equivalent("Bye now.", "bye now", "c" * 64, "d" * 64, name="two")
    files = sorted((tmp_path / "judge").glob("*.json"))
    assert len(files) == 2
    assert not (tmp_path / "judge_verdicts.json").exists()
    for f in files:
        text = f.read_bytes().decode("utf-8")
        assert text.endswith("}\n") and "\r\n" not in text
    assert {e["behavior"] for e in _ledger_entries(tmp_path)} == {"one", "two"}


def test_two_branches_rulings_merge_without_conflict(tmp_path):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

    def sh(*args, **env):
        r = subprocess.run([*git, *args] if args[0] != "nw" else
                           [sys.executable, "-m", "nightward", *args[1:]],
                           cwd=tmp_path, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_env(**env))
        return r

    (tmp_path / "test_n.py").write_text(
        "import os\n"
        "def test_a(behavior):\n"
        "    behavior('n.a', {'0': 'Refund is on its way.', '1': 'refund is on its way'}"
        "[os.environ.get('A', '0')], semantic=True)\n"
        "def test_b(behavior):\n"
        "    behavior('n.b', {'0': 'Dispute received.', '1': 'dispute received'}"
        "[os.environ.get('B', '0')], semantic=True)\n", encoding="utf-8")
    _pyproject(tmp_path, "persona:editor")
    assert sh("init", "-q", "-b", "main").returncode == 0
    assert sh("nw", "init").returncode == 0
    assert sh("nw", "run", ".").returncode == 0
    assert sh("nw", "review").returncode == 0  # D26: only review marks
    assert sh("nw", "approve", "--all").returncode == 0
    sh("add", "-A")
    assert sh("commit", "-qm", "base").returncode == 0
    sh("checkout", "-qb", "feat-a")
    assert "intact" in sh("nw", "run", ".", A="1").stdout
    sh("add", "-A")
    sh("commit", "-qm", "a")
    sh("checkout", "-q", "main")
    sh("checkout", "-qb", "feat-b")
    assert "intact" in sh("nw", "run", ".", B="1").stdout
    sh("add", "-A")
    sh("commit", "-qm", "b")
    sh("checkout", "-q", "main")
    assert sh("merge", "-q", "feat-a").returncode == 0
    m = sh("merge", "-q", "--no-edit", "feat-b")
    assert m.returncode == 0, m.stdout + m.stderr
    assert len(list((tmp_path / ".nightward" / "judge").glob("*.json"))) == 2


def test_conflicted_legacy_ledger_is_named_a_merge_conflict(tmp_path):
    ledger = tmp_path / "judge_verdicts.json"
    ledger.write_text('{\n<<<<<<< HEAD\n  "a": {}\n=======\n  "b": {}\n>>>>>>> feat\n}\n',
                      encoding="utf-8")
    with pytest.raises(NightwardError, match="merge conflict markers") as exc:
        Judge("anthropic:claude-haiku-4-5", cache_path=ledger)
    assert "both sides" in str(exc.value) and "delete" not in str(exc.value)


def test_conflicted_ruling_file_is_named_a_merge_conflict(tmp_path):
    (tmp_path / "judge").mkdir()
    (tmp_path / "judge" / "abc.json").write_text(
        '<<<<<<< HEAD\n{"verdict": "SAME"}\n=======\n{"verdict": "DIFFERENT"}\n>>>>>>> b\n',
        encoding="utf-8")
    with pytest.raises(NightwardError, match="merge conflict markers"):
        Judge("anthropic:claude-haiku-4-5", cache_path=tmp_path / "judge_verdicts.json")


def test_legacy_ledger_api_rulings_still_replay(tmp_path):
    spec = "anthropic:claude-haiku-4-5"
    (tmp_path / "judge_verdicts.json").write_text(
        json.dumps({f"f1:f2:{spec}": {"verdict": "SAME", "reason": "r"}}), encoding="utf-8")
    v = Judge(spec, cache_path=tmp_path / "judge_verdicts.json").equivalent("a", "b", "f1", "f2")
    assert v.verdict == SAME and v.cached


# --- R3-LLM-03 (D22): the judge comes from the store's project -----------------


def _monorepo(tmp_path):
    _pyproject(tmp_path, "persona:editor", '[tool.pytest.ini_options]\ntestpaths = ["services"]\n')
    chat = tmp_path / "services" / "chat"
    chat.mkdir(parents=True)
    (chat / "pyproject.toml").write_text('[project]\nname = "chat"\n', encoding="utf-8")
    (chat / "test_chat.py").write_text(
        "import os\n"
        "def test_reply(behavior):\n"
        "    text = 'your refund was approved' if os.environ.get('V') else "
        "'Your refund was approved.'\n"
        "    behavior('chat.reply', text, group='chat', semantic=True)\n", encoding="utf-8")
    assert cli("run", ".", cwd=tmp_path, env=_env()).returncode == 0
    cli("review", cwd=tmp_path)   # D26: only review marks
    assert cli("approve", "--all", cwd=tmp_path, env=_env()).returncode == 0


def test_running_a_subpackage_keeps_the_store_projects_judge(tmp_path):
    _monorepo(tmp_path)
    r = cli("run", "services/chat", cwd=tmp_path, env=_env(V="1"))
    assert "intact" in r.stdout, r.stdout + r.stderr
    assert "persona:editor" in r.stdout and "pyproject.toml" in r.stdout
    assert os.path.join("services", "chat") not in r.stdout.split("judge:")[1].splitlines()[0]


def test_mcp_run_of_a_subpackage_keeps_the_store_projects_judge(tmp_path, monkeypatch):
    from nightward import mcp_server
    _monorepo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    monkeypatch.setenv("V", "1")
    out = mcp_server.run_tool(path="services/chat")
    assert out["boundary"] == "intact" and out["judge"]["spec"] == "persona:editor"


def test_a_sub_pyproject_judge_cannot_swap_the_projects_judge(tmp_path, monkeypatch):
    from nightward import mcp_server
    _monorepo(tmp_path)
    _pyproject(tmp_path / "services" / "chat", "persona:lenient")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    out = mcp_server.run_tool(path="services/chat")
    assert out["judge"] is None or out["judge"]["spec"] != "persona:lenient"


def test_judge_setting_names_the_file_it_came_from(tmp_path):
    from nightward.config import judge_setting
    _pyproject(tmp_path, "persona:editor")
    spec, source = judge_setting(tmp_path / ".nightward")
    assert spec == "persona:editor" and source == tmp_path / "pyproject.toml"


# --- R3-FIN-08: approved semantic behaviors without a judge say so -------------


def test_semantic_change_without_a_judge_says_why_it_was_compared_exactly(tmp_path):
    _approved(tmp_path, judge=None)
    r = cli("run", ".", cwd=tmp_path, env=_env(REPLY=REWORDED))
    assert "breached" in r.stdout
    assert "no judge configured" in r.stderr and "[tool.nightward]" in r.stderr
    assert "1 approved semantic behavior(s) compared exactly" in r.stderr
    status = json.loads(cli("status", "--json", cwd=tmp_path).stdout)
    assert status["judge"]["spec"] is None
    assert status["judge"]["compared_exactly"] == ["reply.refund"]


def test_no_judge_note_is_silent_without_semantic_mismatches(tmp_path):
    _approved(tmp_path, judge=None)
    r = cli("run", ".", cwd=tmp_path, env=_env())
    assert "no judge configured" not in r.stderr


def test_mcp_run_carries_the_no_judge_note(tmp_path, monkeypatch):
    from nightward import mcp_server
    _approved(tmp_path, judge=None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    monkeypatch.setenv("REPLY", REWORDED)
    out = mcp_server.run_tool()
    assert out["boundary"] == "breached"
    assert "no judge configured" in out["judge"]["unavailable"]



# --- R3-LLM-07: the MCP server pins its store; results name it ------------------


def _gate_project(tmp_path):
    _approved(tmp_path)
    store = tmp_path / ".nightward"
    forged = tmp_path / ".pytest_cache" / "nw"
    import shutil
    shutil.copytree(store, forged)
    return store, forged


def test_mcp_refuses_a_store_other_than_the_one_the_server_was_started_with(
        tmp_path, monkeypatch):
    from nightward import mcp_server
    store, forged = _gate_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    monkeypatch.setattr(mcp_server, "_server_dir", None)
    mcp_server.configure(dir=".nightward")
    with pytest.raises(NightwardError, match="nightward mcp --dir"):
        mcp_server.run_tool(dir=".pytest_cache/nw")
    with pytest.raises(NightwardError, match="nightward mcp --dir"):
        mcp_server.status_tool(dir=str(forged))
    out = mcp_server.run_tool(dir=str(store))           # the same store, spelled otherwise
    assert out["store"] == str(store.resolve()) and out["path"] == "."


def test_mcp_results_name_the_store_and_path(tmp_path, monkeypatch):
    from nightward import mcp_server
    _approved(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server, "_server_judge", None)
    monkeypatch.setattr(mcp_server, "_server_dir", None)
    mcp_server.configure()
    out = mcp_server.run_tool()
    assert out["store"] == str((tmp_path / ".nightward").resolve()) and out["path"] == "."
    assert mcp_server.status_tool()["store"] == out["store"]


def test_cli_mcp_takes_the_store_option():
    from typer.testing import CliRunner

    from nightward.cli import app
    r = CliRunner().invoke(app, ["mcp", "--help"])
    assert "--dir" in r.output
