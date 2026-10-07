"""Beta round 3: doctor's advice equalizes the noise it diagnoses (float rounding,
reordered rows with float drift, reformatted JSON strings), shows where a long
string differs, and names Unicode normalization changes."""
import json
import math
import os
import random
import struct
import unicodedata

from nightward.core.behavior import Behavior
from nightward.core.doctor import diagnose
from nightward.core.visible import reveal


def _diag(old, new):
    return diagnose({"b": Behavior("b", old)}, {"b": Behavior("b", new)})["behaviors"]["b"]


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _next_f32(x: float) -> float:
    bits = struct.unpack("<I", struct.pack("<f", x))[0]
    return struct.unpack("<f", struct.pack("<I", bits + 1 if x > 0 else bits - 1))[0]


def _digits(note: str) -> int:
    return int(note.split('{x:.')[1].split("g}")[0])


def _vectors(n=3072, seed=42):
    rng = random.Random(seed)
    old = [_f32(rng.uniform(-0.2, 0.2)) for _ in range(n)]
    return old, [_next_f32(x) for x in old]       # another machine's BLAS: 1 ulp


# --- R3-LLM-01: the float advice is verified on the evidence ----------------------


def test_float32_rounding_advice_equalizes_every_drifted_value():
    old, new = _vectors()
    [f] = _diag(old, new)
    assert f["kind"] == "float-noise" and f["count"] == len(old)
    d = _digits(f["note"])
    assert all(float(f"{a:.{d}g}") == float(f"{b:.{d}g}") for a, b in zip(old, new, strict=True))
    assert "boundary" in f["note"]          # honest: lowers the odds, no guarantee


def test_values_rounded_by_the_advice_that_still_flip_are_named_as_boundary_flips():
    old, new = _vectors()
    r6 = [[float(f"{x:.6g}") for x in old]], [[float(f"{x:.6g}") for x in new]]
    found = _diag(*r6)
    assert found and all(f["kind"] != "changed" or "rounding boundary" in f["note"]
                         for f in found)
    [f] = found
    d = _digits(f["note"])
    assert d < 6
    assert all(float(f"{a:.{d}g}") == float(f"{b:.{d}g}")
               for a, b in zip(r6[0][0], r6[1][0], strict=True))


def test_a_one_cent_price_change_still_looks_real_first():
    [f] = _diag({"price": 1234.56}, {"price": 1234.57})
    assert f["path"] == "price" and f["note"].startswith("looks like a real change")


# --- R3-DATA-04: reordered rows with float drift --------------------------------


def _mart(run2: bool):
    rows = [[cust, 400, cust * 1.37 + 0.1] for cust in range(200)]
    if run2:
        random.Random(1).shuffle(rows)
        for r in rows[::3]:
            r[2] = math.nextafter(r[2], math.inf)
    return rows


def test_reordered_rows_with_float_noise_are_named_as_both():
    [f] = _diag(_mart(False), _mart(True))
    assert f["kind"] == "order-only" and f["path"] == "$"
    assert "float64 noise" in f["note"] and "$[*][2]" in f["note"]
    assert "sort" in f["note"]
    d = _digits(f["note"])

    def tamed(rows):
        return sorted([[float(f"{v:.{d}g}") if isinstance(v, float) else v for v in r]
                       for r in rows])
    assert tamed(_mart(False)) == tamed(_mart(True))


def test_reordered_rows_with_a_real_value_change_are_not_called_noise():
    old = _mart(False)
    new = [r[:] for r in old]
    random.Random(1).shuffle(new)
    new[0][2] += 5.0
    assert all(f["kind"] != "order-only" for f in _diag(old, new))


def test_pure_reorder_keeps_its_plain_note():
    old = [[1, "a"], [2, "b"]]
    [f] = _diag(old, old[::-1])
    assert f["kind"] == "order-only" and "noise" not in f["note"]


# --- R3-LLM-06: a JSON string that only changed its formatting ------------------


def test_reformatted_json_arguments_are_named_as_formatting_only():
    old = {"name": "file_invoice",
           "arguments": '{"invoice_id": "INV-77", "notify": true, "channels": ["email"]}'}
    new = {"name": "file_invoice",
           "arguments": '{"notify":true,"invoice_id":"INV-77","channels":["email"]}'}
    [f] = _diag(old, new)
    assert f["path"] == "arguments" and f["kind"] == "json-format"
    assert "json.loads" in f["note"]


def test_a_real_change_inside_a_json_string_names_the_inner_path():
    old = {"arguments": '{"invoice_id": "INV-77", "notify": true}'}
    new = {"arguments": '{"invoice_id": "INV-78", "notify": true}'}
    [f] = _diag(old, new)
    assert f["path"] == "arguments<json>.invoice_id" and f["kind"] == "changed"
    assert f["detail"] == '"INV-77" -> "INV-78"'


# --- R3-DATA-05: the snippet shows where a long string differs ------------------


def test_long_string_snippet_is_centered_on_the_difference():
    old = "2024-03-01,KR,12,76.22,super long description field here,1520.5"
    new = "2024-03-01,KR,12,76.22,super long description field here,1250.5"
    [f] = _diag(old, new)
    before, after = f["detail"].split(" -> ")
    assert before != after
    assert "1520.5" in before and "1250.5" in after


def test_json_partition_file_names_the_changed_field():
    old = json.dumps({"avg_amount": 76.22, "country": "KR", "revenue": 1520.5}, sort_keys=True)
    new = json.dumps({"avg_amount": 76.22, "country": "KR", "revenue": 1250.5}, sort_keys=True)
    [f] = _diag(old, new)
    assert f["path"] == "$<json>.revenue" and f["detail"] == "1520.5 -> 1250.5"


# --- R3-LLM-05: a Unicode normalization change is named, not escaped -------------


KO = "고객님의 환불이 승인되었습니다."


def test_doctor_names_an_nfc_to_nfd_change():
    [f] = _diag(KO, unicodedata.normalize("NFD", KO))
    assert "normalization" in f["note"] and 'normalize("NFC"' in f["note"]


def test_review_names_an_nfc_to_nfd_change_without_escaping_every_character():
    shown = reveal(KO, unicodedata.normalize("NFD", KO))
    assert shown is not None
    old, new, note = shown
    assert "\\u" not in old + new
    assert "NFC -> NFD" in note and "normalization" in note


def test_cli_doctor_prints_every_new_finding_kind(tmp_path):
    # Every kind doctor can emit needs a mark in the CLI (a KeyError once).
    import subprocess
    import sys

    from nightward.cli import _DOCTOR_MARKS
    from nightward.core import doctor
    kinds = {v for k, v in vars(doctor).items() if k.isupper() and isinstance(v, str)
             and k not in ("ROOT",) and not k.startswith("_")}
    assert kinds <= set(_DOCTOR_MARKS)
    test = tmp_path / "test_t.py"
    body = ("import os\ndef test_t(behavior):\n"
            "    behavior('t', {'arguments': os.environ.get('A', '{\"a\": 1, \"b\": 2}')})\n")
    test.write_text(body, encoding="utf-8")

    def nw(*args, **env):
        return subprocess.run([sys.executable, "-m", "nightward", *args], cwd=tmp_path,
                              capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, **env})
    nw("run", ".")
    nw("approve", "--all")
    nw("run", ".", A='{"b":2,"a":1}')
    r = nw("doctor")
    assert r.returncode == 0, r.stderr
    assert "~ arguments" in r.stdout and "json.loads" in r.stdout
