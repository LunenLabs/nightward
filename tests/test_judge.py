"""LLM-judge: equivalence only, opt-in only, fail-closed, never approves.

The persona provider gives deterministic key-free judges so every path is
testable without an API key — exactly how a real provider plugs in.
"""
import json
import subprocess
import sys
import textwrap

import pytest

from nightward.core.behavior import Behavior
from nightward.core.blast import aggregate
from nightward.core.diff import CHANGED, UNCHANGED, compare
from nightward.errors import NightwardError
from nightward.judge import DIFFERENT, SAME, Judge, JudgeUnavailable, parse_spec

# --- spec parsing ------------------------------------------------------------


def test_parse_spec_provider_and_model():
    assert parse_spec("persona:editor") == ("persona", "editor")
    assert parse_spec("anthropic:claude-haiku-4-5") == ("anthropic", "claude-haiku-4-5")


@pytest.mark.parametrize("bad", ["editor", "persona:", ":editor", "openai:gpt"])
def test_parse_spec_rejects_bad_specs(bad):
    with pytest.raises(NightwardError):
        parse_spec(bad)


def test_unknown_persona_is_clean_error(tmp_path):
    judge = Judge("persona:nope", cache_path=tmp_path / "c.json")
    with pytest.raises(NightwardError):
        judge.equivalent("a", "b", "f1", "f2")


# --- persona verdicts ----------------------------------------------------------


def _verdict(spec, old, new, tmp_path):
    # cache keys come from fingerprints; derive them from the texts like real use
    return Judge(spec, cache_path=tmp_path / "c.json").equivalent(
        old, new, f"fp-{hash(old)}", f"fp-{hash(new)}")


def test_persona_lenient_always_same(tmp_path):
    assert _verdict("persona:lenient", "totally", "different", tmp_path).verdict == SAME


def test_persona_strict_always_different(tmp_path):
    assert _verdict("persona:strict", "same-ish", "same-ish!", tmp_path).verdict == DIFFERENT


def test_persona_editor_normalized_equality(tmp_path):
    same = _verdict("persona:editor", "The Total is 42.", "the total   is 42", tmp_path)
    diff = _verdict("persona:editor", "The total is 42.", "The total is 43.", tmp_path)
    assert same.verdict == SAME
    assert diff.verdict == DIFFERENT


# --- cache: one ruling per fingerprint pair, persisted ------------------------


def test_cache_prevents_rejudging_and_persists(tmp_path, monkeypatch):
    calls = []

    def counting_backend(model, old, new):
        calls.append(model)
        return SAME, "counted"

    import nightward.judge as judge_mod
    monkeypatch.setitem(judge_mod._BACKENDS, "persona", counting_backend)

    ledger = tmp_path / "judge_verdicts.json"
    j1 = Judge("persona:editor", cache_path=ledger)
    v1 = j1.equivalent("a", "b", "fp-old", "fp-new", name="daily_brief")
    v2 = j1.equivalent("a", "b", "fp-old", "fp-new", name="daily_brief")
    assert (v1.cached, v2.cached) == (False, True)

    # a fresh Judge instance (e.g. a later `approve` recompute) reuses the file
    j2 = Judge("persona:editor", cache_path=ledger)
    assert j2.equivalent("a", "b", "fp-old", "fp-new").cached is True
    assert len(calls) == 1
    entry = json.loads(ledger.read_text(encoding="utf-8"))["fp-old:fp-new:persona:editor"]
    # the ledger is committed and human-reviewed: entries must be self-describing
    assert entry["behavior"] == "daily_brief"
    assert entry["model"] == "persona:editor"


def test_ledger_replays_verdicts_without_any_backend(tmp_path, monkeypatch):
    """The committed ledger must keep a judged-SAME boundary intact on a fresh
    clone / CI runner where the backend is unavailable (no key, no network)."""
    import nightward.judge as judge_mod

    ledger = tmp_path / "judge_verdicts.json"
    baseline, pending = _pair("old wording", "new wording")
    assert compare(baseline, pending,
                   judge=Judge("persona:lenient", cache_path=ledger))[0].kind == UNCHANGED

    def down(model, old, new):
        raise JudgeUnavailable("fresh CI runner: no key")

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", down)
    replayed = Judge("persona:lenient", cache_path=ledger)  # fresh process, ledger only
    [change] = compare(baseline, pending, judge=replayed)
    assert change.kind == UNCHANGED  # deterministic without re-judging
    assert change.judged is True


def test_unavailable_backend_returns_none(tmp_path, monkeypatch):
    import nightward.judge as judge_mod

    def down(model, old, new):
        raise JudgeUnavailable("no key")

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", down)
    judge = Judge("persona:editor", cache_path=tmp_path / "c.json")
    assert judge.equivalent("a", "b", "f1", "f2") is None


# --- compare integration: the gate semantics ----------------------------------


def _pair(old_text, new_text, *, semantic=True):
    baseline = {"summary": Behavior("summary", old_text, group="ai", semantic=semantic)}
    pending = {"summary": Behavior("summary", new_text, group="ai", semantic=semantic)}
    return baseline, pending


def test_judge_same_collapses_to_unchanged_with_audit(tmp_path):
    judge = Judge("persona:lenient", cache_path=tmp_path / "c.json")
    [change] = compare(*_pair("old wording", "new wording"), judge=judge)
    assert change.kind == UNCHANGED
    assert change.judged is True
    assert change.judge_model == "persona:lenient"
    report = aggregate(compare(*_pair("old wording", "new wording"), judge=judge))
    assert report["boundary"] == "intact"
    assert report["counts"]["judged_same"] == 1


def test_judge_different_stays_changed_with_audit(tmp_path):
    judge = Judge("persona:strict", cache_path=tmp_path / "c.json")
    [change] = compare(*_pair("a", "b"), judge=judge)
    assert change.kind == CHANGED
    assert change.judged is True
    d = change.to_dict()
    assert d["judged"] and d["judge_model"] == "persona:strict"


def test_non_semantic_behaviors_never_reach_the_judge(tmp_path, monkeypatch):
    import nightward.judge as judge_mod

    def explode(model, old, new):
        raise AssertionError("judge must not run on deterministic behaviors")

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", explode)
    judge = Judge("persona:lenient", cache_path=tmp_path / "c.json")
    [change] = compare(*_pair("a", "b", semantic=False), judge=judge)
    assert change.kind == CHANGED
    assert change.judged is False


def test_no_judge_keeps_v0_fingerprint_semantics():
    [change] = compare(*_pair("a", "b"))
    assert change.kind == CHANGED
    assert change.judged is False


def test_judge_failure_fails_closed(tmp_path, monkeypatch):
    import nightward.judge as judge_mod

    def down(model, old, new):
        raise JudgeUnavailable("offline")

    monkeypatch.setitem(judge_mod._BACKENDS, "persona", down)
    judge = Judge("persona:lenient", cache_path=tmp_path / "c.json")
    [change] = compare(*_pair("a", "b"), judge=judge)
    assert change.kind == CHANGED  # gate closes loudly, never opens silently
    assert change.judged is False


# --- behavior schema: opt-in flag, backward compatible -------------------------


def test_behavior_semantic_roundtrip_and_compat():
    b = Behavior("x", "text", semantic=True)
    assert Behavior.from_dict(b.to_dict()).semantic is True
    # pre-v0.2 approved files have no "semantic" key and stay byte-stable
    legacy = {"name": "x", "group": None, "payload": "text"}
    assert Behavior.from_dict(legacy).semantic is False
    assert "semantic" not in Behavior("x", "text").to_dict()


# --- end-to-end through the CLI -------------------------------------------------


TEST_FILE = '''
def test_ai_summary(behavior):
    behavior("daily_summary", {SUMMARY!r}, group="ai", semantic=True)
    behavior("item_count", 42, group="facts")
'''


def _cli(*args, cwd):
    return subprocess.run([sys.executable, "-m", "nightward", *args],
                          cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def test_cli_run_with_persona_judge_keeps_boundary_intact(tmp_path):
    test_py = tmp_path / "test_app.py"
    test_py.write_text(
        textwrap.dedent(TEST_FILE.replace("{SUMMARY!r}", repr("market went up today"))),
        encoding="utf-8")
    assert _cli("run", ".", cwd=tmp_path).returncode == 0
    assert _cli("approve", "--all", cwd=tmp_path).returncode == 0

    # reworded AI output, code/facts identical
    test_py.write_text(
        textwrap.dedent(TEST_FILE.replace("{SUMMARY!r}", repr("today the market rose"))),
        encoding="utf-8")

    # without a judge: v0 fingerprint -> breached (the 25/25 FP problem)
    r0 = _cli("run", ".", cwd=tmp_path)
    assert "breached" in r0.stdout

    # with a multi-model judge spec (persona stand-in): intact + audit trail
    r1 = _cli("run", ".", "--judge", "persona:lenient", cwd=tmp_path)
    assert "intact" in r1.stdout
    assert "ruled semantically same" in r1.stdout.lower()
    report = json.loads((tmp_path / ".nightward" / "report.json").read_text(encoding="utf-8"))
    assert report["counts"]["judged_same"] == 1
    assert (tmp_path / ".nightward" / "judge_verdicts.json").exists()


# --- D8: key-free personas fail closed on meaning-bearing characters ------------
# R1-FIN-03 / R1-LLM-02: persona:editor collapsed sign flips, currency swaps,
# decimal separators, operators and emoji because it dropped every non-word char.

MEANING_CHANGES = [
    ("Your balance is -$120.00 after the refund of $19.99.",
     "Your balance is $120.00 after the refund of $19.99."),           # sign flip
    ("Your balance is -$120.00 after the refund of $19.99.",
     "Your balance is -€120.00 after the refund of €19.99."),          # currency swap
    ("Your balance is -$120.00 after the refund of $19.99.",
     "Your balance is -$120,00 after the refund of $19,99."),          # decimal separator
    ("Your loyalty discount is 15% off.", "Your loyalty discount is $15 off."),
    ("def net(price, fee):\n    return price - fee",
     "def net(price, fee):\n    return price + fee"),                  # operator
    ("if user.age >= 18: allow()", "if user.age <= 18: allow()"),      # comparison
    ("if a != b: stop()", "if a = b: stop()"),                         # negated operator
    ("Customer mood: 👍", "Customer mood: 👎"),                          # emoji
    ("The refund was approved.", "The refund was not approved."),     # negation
    ("Output power is 5 mW.", "Output power is 5 MW."),                # unit symbol case
    ("Is the order shipped.", "Is the order shipped?"),                # statement -> question
]


@pytest.mark.parametrize("spec", ["persona:editor", "persona:lenient"])
@pytest.mark.parametrize("old,new", MEANING_CHANGES)
def test_personas_rule_meaning_changes_different(spec, old, new, tmp_path):
    assert _verdict(spec, old, new, tmp_path).verdict == DIFFERENT


@pytest.mark.parametrize("spec", ["persona:editor", "persona:lenient"])
@pytest.mark.parametrize("old,new", [
    ({"amount": 49.99}, {"amount": "49.99"}),                          # number -> string
    ({"items": ["cable", "adapter"]}, {"items": "cable adapter"}),     # list -> string
    ({"n": 1}, {"n": 1.0}),                                            # int -> float
    ({"ok": True}, {"ok": 1}),                                         # bool -> int
    ({"a": "x"}, {"b": "x"}),                                          # key renamed
])
def test_personas_rule_value_type_changes_different(spec, old, new, tmp_path):
    verdict = Judge(spec, cache_path=tmp_path / "c.json").equivalent(old, new, "f1", "f2")
    assert verdict.verdict == DIFFERENT


@pytest.mark.parametrize("old,new", [
    ("The Total is 42.", "the total   is 42"),
    ("Your refund of $50 has been approved.", "your refund of $50 has been approved!"),
    ("Hello, world; bye", "hello world bye"),
    ("결제가 완료되었습니다.", "결제가 완료되었습니다"),
])
def test_editor_collapses_only_cosmetic_rewording(old, new, tmp_path):
    assert _verdict("persona:editor", old, new, tmp_path).verdict == SAME


def test_editor_judges_strings_inside_structured_payloads(tmp_path):
    judge = Judge("persona:editor", cache_path=tmp_path / "c.json")
    same = judge.equivalent({"reply": "Approved.", "n": 2}, {"reply": "approved", "n": 2},
                            "f1", "f2")
    diff = judge.equivalent({"reply": "Approved.", "n": 2}, {"reply": "approved", "n": 3},
                            "f1", "f3")
    assert (same.verdict, diff.verdict) == (SAME, DIFFERENT)


def test_lenient_tolerates_word_changes_but_not_facts(tmp_path):
    assert _verdict("persona:lenient", "market went up today",
                    "today the market rose", tmp_path).verdict == SAME
    assert _verdict("persona:lenient", "Total: 120 USD", "Total: 120 EUR",
                    tmp_path).verdict == DIFFERENT


def test_persona_rulings_from_older_rules_are_rejudged(tmp_path):
    # R1-FIN-03: a committed ledger replayed the old editor's SAME for a sign flip
    # forever. Persona rulings recorded under older rules must not replay.
    ledger = tmp_path / "judge_verdicts.json"
    ledger.write_text(json.dumps({"f1:f2:persona:editor": {
        "verdict": SAME, "reason": "same words modulo case/punctuation/whitespace",
        "behavior": "statement.notice", "model": "persona:editor"}}), encoding="utf-8")
    verdict = Judge("persona:editor", cache_path=ledger).equivalent(
        "Your balance is -$120.00.", "Your balance is $120.00.", "f1", "f2")
    assert (verdict.verdict, verdict.cached) == (DIFFERENT, False)


def test_lenient_keeps_currency_codes_before_numbers(tmp_path):
    assert _verdict("persona:lenient", "Total: USD 120", "Total: EUR 120",
                    tmp_path).verdict == DIFFERENT
