"""Beta round 4 (AI/judge/doctor senior): numbers stay exact in every script
(R4-LLM-01), ledger rulings must describe the pair they decide (R4-LLM-03),
custom scrub rules are test-owned and visible (D28), and doctor's noise advice
is verified on the evidence (R3-DATA-04, R4-DATA-03, R4-DATA-04)."""
import itertools

import pytest

from nightward import judge

E, L = judge._persona_editor, judge._persona_lenient

# --- R4-LLM-01: a separator between digits is part of the number ---------------

DIGITS = {"ascii": "15", "fullwidth": "１５", "arabic-indic": "١٥", "devanagari": "१५",
          "thai": "๑๕", "bengali": "১৫"}
SEPARATORS = [".", ",", ":", ";", "!", "．", "，", "：", "；", "！", "、", "。", "·", "'",
              "٫", "٬"]   # ARABIC DECIMAL / THOUSANDS SEPARATOR
TEMPLATES = ["The fee is {n} times today.", "手数料は{n}倍です。", "退款金额为{n}万元。",
             "수수료는 {n}배입니다."]


def _cases():
    for (script, (one, five)), template in itertools.product(DIGITS.items(), TEMPLATES):
        for a, b in itertools.permutations(SEPARATORS, 2):
            for gap in ("", " "):
                yield (script, template.format(n=f"{one}{a}{gap}{five}"),
                       template.format(n=f"{one}{b}{gap}{five}"))


@pytest.mark.parametrize("persona", [E, L], ids=["editor", "lenient"])
def test_any_change_of_separator_between_digits_is_different(persona):
    same = [(s, o, n) for s, o, n in _cases() if persona(o, n)[0] == judge.SAME]
    assert same == []


@pytest.mark.parametrize("old,new", [
    ("キャンセル手数料は通常料金の１．５倍です。", "キャンセル手数料は通常料金の１，５倍です。"),
    ("返品受付は平日10：30までです。", "返品受付は平日10．30までです。"),
    ("退款金额为1．5万元，三个工作日内到账。", "退款金额为1，5万元，三个工作日内到账。"),
    ("比率は１：５です。", "比率は１．５です。"),
    ("対象は１、５番です。", "対象は１．５番です。"),
    ("The fee is １．５ times.", "The fee is 1.5 times."),          # NFKC-equal, still a change
    ("The fee is 1.5 times.", "The fee is 1,5 times."),
    ("Order 1, 5 and 7 shipped.", "Order 1. 5 and 7 shipped."),
])
@pytest.mark.parametrize("persona", [E, L], ids=["editor", "lenient"])
def test_reported_number_changes_are_different(persona, old, new):
    assert persona(old, new)[0] == judge.DIFFERENT


@pytest.mark.parametrize("old,new", [
    ("返金は承認されました。", "返金は承認されました"),
    ("合計は1,500円です。", "合計は1,500円です"),
    ("The total is 5.", "the total is 5"),
    ("はい、返金は5日以内です。", "はい 返金は5日以内です"),
])
def test_sentence_punctuation_away_from_numbers_stays_cosmetic(old, new):
    assert E(old, new)[0] == judge.SAME

