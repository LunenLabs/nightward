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
              "٫", "٬",   # ARABIC DECIMAL / THOUSANDS SEPARATOR
              # R4-OPS-01 round 5: ratios, ranges and minus signs in every width
              "/", "／", "-", "‐", "–", "—", "−", "~", "～", "〜", "_", "×"]
TEMPLATES = ["The fee is {n} times today.", "手数料は{n}倍です。", "退款金额为{n}万元。",
             "수수료는 {n}배입니다."]
# Whitespace before / after the separator: none, ASCII, NBSP, THIN SPACE, NARROW
# NO-BREAK SPACE, IDEOGRAPHIC SPACE (R4-LLM-01 round 5: `1 : 5` vs `1 . 5`).
GAPS = [("", ""), ("", " "), (" ", ""), (" ", " "), (" ", " "),
        (" ", " "), (" ", ""), ("　", "　")]


def _cases():
    for (script, (one, five)), template in itertools.product(DIGITS.items(), TEMPLATES):
        def n(sep, gap, one=one, five=five, template=template):
            return template.format(n=f"{one}{gap[0]}{sep}{gap[1]}{five}")
        # another separator, same spacing
        for a, b in itertools.permutations(SEPARATORS, 2):
            for gap in GAPS:
                yield script, n(a, gap), n(b, gap)
        # the same separator (or none), other spacing around it
        for sep in ["", *SEPARATORS]:
            for g, h in itertools.permutations(GAPS, 2):
                if n(sep, g) != n(sep, h):   # no separator: " "+"" == ""+" "
                    yield script, n(sep, g), n(sep, h)


def test_the_table_covers_spaced_separators():
    cases = list(_cases())
    assert ("ascii", "The fee is 1 : 5 times today.", "The fee is 1 . 5 times today.") in cases
    assert ("fullwidth", "手数料は１ ．５倍です。", "手数料は１ ，５倍です。") in cases


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
    # round 5: whitespace around the separator
    ("Le plan est à l'échelle 1 : 5 ; merci.", "Le plan est à l'échelle 1 . 5 ; merci."),
    ("当前汇率为 1 ：7 。", "当前汇率为 1 ．7 。"),
    ("手数料は通常料金の １ ．５ 倍です。", "手数料は通常料金の １ ，５ 倍です。"),
    ("Total 1 , 5 today.", "Total 1 . 5 today."),
    ("Open 10 : 30 today.", "Open 10 . 30 today."),
    ("Items 1 , 2 and 3.", "Items 1 2 and 3."),
    ("Pages 1 – 5 today.", "Pages 1 - 5 today."),
    ("Score 1/5 today.", "Score 1:5 today."),
    ("Price 1 000 euros.", "Price 1 000 euros."),
    ("Price 1 000 euros.", "Price 1000 euros."),
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

