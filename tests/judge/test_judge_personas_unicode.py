"""Beta round 3: persona judges and Unicode (R3-LLM-04 case folding, R3-LLM-05
CJK punctuation and negation), and the datetime capture hint (R3-DATA-01)."""
import datetime

import pytest

from nightward import judge
from nightward.core.behavior import canonical_json
from nightward.errors import NightwardError

E, L = judge._persona_editor, judge._persona_lenient


# --- R3-LLM-04: case-insensitive is not casefold --------------------------------


@pytest.mark.parametrize("old,new", [
    ("Bitte beachten Sie: Alkohol nur in Maßen trinken.",
     "Bitte beachten Sie: Alkohol nur in Massen trinken."),
    ("Die Buße beträgt zehn Euro.", "Die Busse beträgt zehn Euro."),
    ("Die STRAẞE ist gesperrt.", "Die STRASSE ist gesperrt."),
    ("The ﬁle was saved.", "The file was saved."),                # ligature
    ("Approved by the King.", "Approved by the King."),        # KELVIN SIGN
])
def test_spellings_that_only_fold_together_are_different(old, new):
    # lenient lets any ordinary word change, so only editor is in question here
    assert E(old, new)[0] == judge.DIFFERENT


@pytest.mark.parametrize("old,new", [
    ("Alkohol nur in Maßen trinken.", "alkohol nur in maßen trinken"),
    ("Ödeme onaylandı.", "ödeme onaylandı"),
    ("Η παραγγελία στάλθηκε.", "η παραγγελία στάλθηκε"),
])
def test_true_case_changes_stay_same_for_editor(old, new):
    assert E(old, new)[0] == judge.SAME


# --- R3-LLM-05: CJK punctuation, spacing and negation -----------------------------


@pytest.mark.parametrize("old,new", [
    ("返金は承認されました。", "返金は承認されました"),
    ("はい、返金は承認されました。", "はい、 返金は承認されました。"),
    ("退款已批准。", "退款已批准"),
    ("はい、返金は承認されました。", "はい、　返金は承認されました。"),   # ideographic space
])
def test_editor_treats_cjk_sentence_punctuation_and_spaces_as_cosmetic(old, new):
    assert E(old, new)[0] == judge.SAME


@pytest.mark.parametrize("old,new", [
    ("返金は承認されました。", "返金は承認されませんでした。"),
    ("返金は承認されました。", "返金は却下されました。"),
    ("返金は承認されましたか？", "返金は承認されました。"),
])
def test_editor_still_compares_every_cjk_character(old, new):
    assert E(old, new)[0] == judge.DIFFERENT


@pytest.mark.parametrize("old,new", [
    ("환불이 승인되었습니다.", "환불이 승인되지 않았습니다."),
    ("추가 수수료가 있습니다.", "추가 수수료가 없습니다."),
    ("환불이 가능합니다.", "환불이 불가능합니다."),
    ("返金は 承認されました。", "返金は 承認されませんでした。"),
    ("返金できます。", "返金できない。"),
    ("退款已批准。", "退款未批准。"),
])
def test_lenient_fails_closed_on_korean_japanese_chinese_negation(old, new):
    assert L(old, new)[0] == judge.DIFFERENT


def test_cjk_identifier_like_strings_stay_exact():
    assert E("注文ID:ABC-123", "注文id:ABC-123")[0] == judge.DIFFERENT


# --- R3-DATA-01: the datetime hint names the default scrubber ---------------------


@pytest.mark.parametrize("value", [datetime.datetime(2024, 3, 31, 9, 0)])
def test_datetime_hint_warns_that_iso_datetimes_are_masked(value):
    with pytest.raises(NightwardError) as exc:
        canonical_json({"due": value})
    msg = str(exc.value)
    assert ".isoformat()" in msg and "scrub=False" in msg and "disable_defaults" in msg


def test_date_hint_has_no_scrub_warning():
    with pytest.raises(NightwardError) as exc:
        canonical_json({"due": datetime.date(2024, 3, 31)})
    assert ".isoformat()" in str(exc.value) and "scrub=False" not in str(exc.value)


def test_pandas_timestamp_hint_warns_too():
    pd = pytest.importorskip("pandas")
    with pytest.raises(NightwardError, match="scrub=False"):
        canonical_json({"due": pd.Timestamp("2024-03-01 09:00")})
