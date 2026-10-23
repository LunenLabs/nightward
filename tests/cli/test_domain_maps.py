"""Domain maps: the rules a behavior guards, next to its diff.

docs/superpowers/specs/2026-10-20-review-depth-and-onboarding-design.md, part 2.
The core only reads maps; a bad map warns and never blocks a review.
"""
import os
import subprocess
import sys

from nightward import domain
from nightward.core.baseline import Store
from nightward.core.behavior import Behavior
from nightward.runner import recompute

MAP = '''+++
group = "pay"
derived_from = "abc1234"

[rules.R1]
rule = "A late FAILED webhook never moves a paid payment back"
code = "app/pay.py:851"
intent = "docstring of handle_webhook"

[rules.R2]
rule = "Payment is prepared only after the walker accepts"

[behaviors]
"pay.webhook" = ["R1", "R2"]
"pay.prepare" = "R2"
+++

# Payment

Free prose for humans: the state matrix, suspected defects, open questions.
'''


def cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "nightward", *args],
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )


def project(tmp_path, maps: dict[str, str], depth: str = "detailed", setting: str = ""):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.nightward]\n{setting}\n[tool.nightward.review.groups]\npay = "{depth}"\n',
        encoding="utf-8")
    folder = tmp_path / "docs" / "nightward-domain"
    folder.mkdir(parents=True)
    for name, text in maps.items():
        (folder / name).write_text(text, encoding="utf-8")
    return tmp_path


def test_rules_load_by_behavior(tmp_path):
    maps = domain.load(project(tmp_path, {"pay.md": MAP}) / ".nightward")
    assert maps.warnings == []
    assert [r.id for r in maps.of("pay.webhook")] == ["R1", "R2"]
    r1, r2 = maps.of("pay.webhook")
    assert (r1.code, r1.intent, r1.source) == ("app/pay.py:851", "docstring of handle_webhook",
                                               "docs/nightward-domain/pay.md")
    assert r2.intent is None                              # unconfirmed
    assert [r.id for r in maps.of("pay.prepare")] == ["R2"]
    assert maps.of("other") == []


def test_no_folder_and_prose_only_maps_are_empty(tmp_path):
    assert domain.load(tmp_path / ".nightward").rules == {}
    maps = domain.load(project(tmp_path, {"notes.md": "# just prose\n"}) / ".nightward")
    assert (maps.rules, maps.warnings) == ({}, [])


def test_a_bad_map_warns_and_the_others_still_load(tmp_path):
    maps = domain.load(project(tmp_path, {
        "a.md": "+++\nrules = [\n+++\n",                     # broken TOML
        "b.md": "+++\nrules = 1\n",                          # no closing fence
        "c.md": '+++\n[behaviors]\n"x" = ["R9"]\n+++\n',     # unknown rule
        "pay.md": MAP,
    }) / ".nightward")
    assert [r.id for r in maps.of("pay.webhook")] == ["R1", "R2"]
    text = "\n".join(maps.warnings)
    assert "a.md" in text and "b.md" in text and "R9" in text


def test_the_folder_is_configurable_relative_to_the_pyproject(tmp_path):
    project(tmp_path, {}, setting='domain = "rules"')
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "pay.md").write_text(MAP, encoding="utf-8")
    assert [r.id for r in domain.load(tmp_path / ".nightward").of("pay.prepare")] == ["R2"]


def changed_store(tmp_path):
    store = Store(tmp_path / ".nightward")
    store.ensure()
    store.write_pending(Behavior(name="pay.webhook", payload={"to": "paid"}, group="pay"))
    store.approve("pay.webhook")
    store.write_pending(Behavior(name="pay.webhook", payload={"to": "pending"}, group="pay"))
    recompute(store)


def test_review_prints_the_rules_above_the_diff_at_detailed(tmp_path):
    project(tmp_path, {"pay.md": MAP})
    changed_store(tmp_path)
    r = cli("review", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "rule R1 A late FAILED webhook never moves a paid payment back" in out
    assert "intent: docstring of handle_webhook" in out
    assert "rule R2" in out and "unconfirmed: inferred from the code alone" in out
    assert out.index("rule R1") < out.index('"to": "pending"')


def test_standard_review_doesnt_print_rules(tmp_path):
    project(tmp_path, {"pay.md": MAP}, depth="standard")
    changed_store(tmp_path)
    r = cli("review", cwd=tmp_path)
    assert "rule R1" not in r.stdout and '"to": "pending"' in r.stdout


def test_a_bad_map_warns_but_review_still_works(tmp_path):
    project(tmp_path, {"broken.md": "+++\nx = [\n+++\n"})
    changed_store(tmp_path)
    r = cli("review", cwd=tmp_path)
    assert r.returncode == 0 and "broken.md" in r.stderr and '"to": "pending"' in r.stdout
