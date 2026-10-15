"""Beta round 4, R4-LLM-03: a model-judge ruling replays only when its readable
fields describe the exact pair it decides; anything else fails closed."""
import json

import pytest

from nightward.core.behavior import Behavior
from nightward.core.diff import CHANGED, UNCHANGED, compare
from nightward.judge import Judge, JudgeUnavailable

# --- R4-LLM-03: a replayed ruling must describe the pair it decides --------------

SPEC = "anthropic:claude-haiku-4-5"
OLD = "Your refund of $250.00 needs a manager's review."
NEW = "Your refund of $250.00 has been approved."


def _pair():
    return ({"reply.T-2": Behavior("reply.T-2", OLD, semantic=True)},
            {"reply.T-2": Behavior("reply.T-2", NEW, semantic=True)})


def _key():
    b, p = _pair()
    return f"{b['reply.T-2'].fingerprint()}:{p['reply.T-2'].fingerprint()}:{SPEC}"


def _no_key(monkeypatch):
    import nightward.judge as judge_mod

    def down(model, old, new):
        raise JudgeUnavailable("ANTHROPIC_API_KEY not set")
    monkeypatch.setitem(judge_mod._BACKENDS, "anthropic", down)


def _genuine(tmp_path, monkeypatch, verdict="SAME"):
    import nightward.judge as judge_mod
    monkeypatch.setitem(judge_mod._BACKENDS, "anthropic", lambda m, o, n: (verdict, "r"))
    compare(*_pair(), judge=Judge(SPEC, cache_path=tmp_path / "judge_verdicts.json"))
    [f] = (tmp_path / "judge").glob("*.json")
    return f


def _judge_again(tmp_path):
    j = Judge(SPEC, cache_path=tmp_path / "judge_verdicts.json")
    [c] = compare(*_pair(), judge=j)
    return c, j


def test_a_genuine_model_ruling_still_replays(tmp_path, monkeypatch):
    _genuine(tmp_path, monkeypatch)
    _no_key(monkeypatch)
    c, j = _judge_again(tmp_path)
    assert c.kind == UNCHANGED and c.judge_replayed
    assert "ledger_rejected" not in j.summary()


@pytest.mark.parametrize("field,value", [
    ("behavior", "reply.T-3"),
    ("old", "Your order ORD-7781 is on its way."),
    ("new", "your order ORD-7781 is on its way"),
    ("model", "anthropic:claude-opus-4"),
])
def test_an_entry_whose_readable_fields_describe_another_change_is_not_replayed(
        tmp_path, monkeypatch, field, value):
    f = _genuine(tmp_path, monkeypatch)
    entry = json.loads(f.read_text(encoding="utf-8")) | {field: value}
    f.write_text(json.dumps(entry), encoding="utf-8")
    _no_key(monkeypatch)
    c, j = _judge_again(tmp_path)
    assert c.kind == CHANGED and not c.judged          # fail closed: compared exactly
    [why] = j.summary()["ledger_rejected"]
    assert f.name in why and "reply.T-2" in why and field in why


def test_an_entry_in_a_file_not_named_by_its_key_is_not_replayed(tmp_path, monkeypatch):
    f = _genuine(tmp_path, monkeypatch)
    f.rename(f.with_name("3e5b0c1d9a7f4e2b8c6d0a1f2e3b4c5d.json"))
    _no_key(monkeypatch)
    c, j = _judge_again(tmp_path)
    assert c.kind == CHANGED
    assert "file name" in j.summary()["ledger_rejected"][0]


def test_a_forged_file_does_not_shadow_the_genuine_one(tmp_path, monkeypatch):
    f = _genuine(tmp_path, monkeypatch, verdict="DIFFERENT")
    entry = json.loads(f.read_text(encoding="utf-8")) | {"verdict": "SAME"}
    forged = tmp_path / "judge" / "zzzz.json"         # sorts after the genuine file
    forged.write_text(json.dumps(entry), encoding="utf-8")
    _no_key(monkeypatch)
    c, _ = _judge_again(tmp_path)
    assert c.kind == CHANGED and c.judged and c.judge_replayed   # the genuine DIFFERENT


def test_a_rejected_entry_is_ruled_again_when_the_model_is_available(tmp_path, monkeypatch):
    import nightward.judge as judge_mod
    f = _genuine(tmp_path, monkeypatch)
    f.write_text(json.dumps(json.loads(f.read_text(encoding="utf-8"))
                            | {"behavior": "reply.T-3"}), encoding="utf-8")
    monkeypatch.setitem(judge_mod._BACKENDS, "anthropic", lambda m, o, n: ("DIFFERENT", "no"))
    c, _ = _judge_again(tmp_path)
    assert c.kind == CHANGED and c.judged and not c.judge_replayed
    assert json.loads(f.read_text(encoding="utf-8"))["behavior"] == "reply.T-2"   # rewritten


def test_legacy_ledger_entries_replay_only_when_they_describe_the_pair(tmp_path, monkeypatch):
    from nightward.judge import _excerpt
    _no_key(monkeypatch)
    legacy = tmp_path / "judge_verdicts.json"
    legacy.write_text(json.dumps({_key(): {"verdict": "SAME", "reason": "r"}}),
                      encoding="utf-8")
    c, j = _judge_again(tmp_path)
    assert c.kind == CHANGED and j.summary()["ledger_rejected"]
    legacy.write_text(json.dumps({_key(): {
        "verdict": "SAME", "reason": "r", "behavior": "reply.T-2", "model": SPEC,
        "old": _excerpt(OLD), "new": _excerpt(NEW)}}), encoding="utf-8")
    c, _ = _judge_again(tmp_path)
    assert c.kind == UNCHANGED and c.judge_replayed
    # migrated into the reviewable one-file-per-ruling layout
    [f] = (tmp_path / "judge").glob("*.json")
    assert json.loads(f.read_text(encoding="utf-8"))["key"] == _key()


def test_cli_run_warns_about_a_forged_ruling_and_stays_breached(tmp_path):
    import os
    import subprocess
    import sys
    (tmp_path / "test_r.py").write_text(
        "import os\n"
        "def test_r(behavior):\n"
        f"    behavior('reply.T-2', os.environ.get('R', {OLD!r}), semantic=True)\n",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    def nw(*args, **extra):
        return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=tmp_path,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env={**env, **extra})
    nw("init")
    nw("run", ".")
    nw("review")                       # D26: approve what was shown
    nw("approve", "--all")
    nw("run", ".", "--judge", SPEC, R=NEW)
    (tmp_path / ".nightward" / "judge").mkdir()
    (tmp_path / ".nightward" / "judge" / "3e5b0c1d9a7f4e2b8c6d0a1f2e3b4c5d.json").write_text(
        json.dumps({"key": _key(), "behavior": "reply.T-3", "model": SPEC, "verdict": "SAME",
                    "old": "Your order ORD-7781 is on its way.",
                    "new": "your order ORD-7781 is on its way", "reason": "case only"}),
        encoding="utf-8")
    r = nw("run", ".", "--judge", SPEC, R=NEW)
    assert "breached" in r.stdout
    assert "3e5b0c1d9a7f4e2b8c6d0a1f2e3b4c5d.json" in r.stderr and "not replayed" in r.stderr
