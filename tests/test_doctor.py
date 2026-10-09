"""doctor calls a drift volatile only on evidence, suggests the narrowest rule
that hides exactly that drift, and never suggests scrubbing a real change."""
import re

from nightward import scrub
from nightward.core.behavior import Behavior, canonical_json
from nightward.core.doctor import (
    DATE,
    FLOAT,
    ORDER,
    REAL,
    STRUCTURAL,
    VOLATILE,
    diagnose,
    findings,
)


def _b(name: str, payload, group=None) -> Behavior:
    return Behavior(name=name, payload=payload, group=group)


def _diag(old: dict, new: dict, **stable):
    """diagnose() over {name: payload} maps, plus unchanged behaviors."""
    baseline = {n: _b(n, p) for n, p in {**old, **stable}.items()}
    pending = {n: _b(n, p) for n, p in {**new, **stable}.items()}
    return diagnose(baseline, pending)


def _kinds(found):
    return {f["path"]: f["kind"] for f in found}


def _apply(rule: str, payload):
    """Run a suggested conftest line against a payload, as the plugin would."""
    exec(compile(rule, "conftest.py", "exec"), {"scrub": scrub})   # a conftest line (D28)
    try:
        return scrub.scrub(payload)
    finally:
        scrub._reset()


def test_findings_structural_changes():
    # added key, list growth, container-type flip, and a scalar type change
    found = findings(
        {"a": 1, "items": [1], "meta": {"x": 1}, "amount": 49.99},
        {"a": 1, "b": 2, "items": [1, 2], "meta": [1], "amount": "49.99"},
    )
    assert _kinds(found) == {"amount": STRUCTURAL, "b": STRUCTURAL,
                             "items": STRUCTURAL, "meta": STRUCTURAL}


def test_one_off_value_change_is_a_real_change_not_volatile():
    # R1-LLM-08 / R1-DATA-04: one before/after pair is no evidence of volatility.
    diag = _diag({"x": {"at": "run-1", "total": 5, "request_id": 17}},
                 {"x": {"at": "run-2", "total": 5, "request_id": 18}})
    assert _kinds(diag["behaviors"]["x"]) == {"at": REAL, "request_id": REAL}
    assert diag["suggestions"] == []


def test_number_to_string_is_structural():
    # R1-LLM-08: {"amount": 49.99} -> {"amount": "49.99"} was called volatile.
    diag = _diag({"inv": {"amount": 49.99}}, {"inv": {"amount": "49.99"}})
    assert _kinds(diag["behaviors"]["inv"]) == {"amount": STRUCTURAL}
    assert diag["suggestions"] == []


def test_changed_chat_message_is_never_masked():
    # R1-LLM-08: one assistant turn switched language; doctor suggested
    # register_field("content"), masking every message body.
    old = [{"role": "assistant", "content": "please repeat that 😀 " * 20}] * 3
    new = [dict(t) for t in old]
    new[1] = {"role": "assistant", "content": "다시 말씀해 주세요 😀 " * 20}
    diag = _diag({"chat": old}, {"chat": new})
    assert _kinds(diag["behaviors"]["chat"]) == {"$[1].content": REAL}
    assert diag["suggestions"] == []


def test_float_last_digit_drift_suggests_rounding_not_masking():
    # R1-DATA-04 case 1: an equivalent refactor moved F1 by one ulp.
    diag = _diag({"churn_f1": {"f1": 0.8215053763440859}},
                 {"churn_f1": {"f1": 0.821505376344086}})
    [f] = diag["behaviors"]["churn_f1"]
    assert f["path"] == "f1" and "round" in f["note"]
    assert diag["suggestions"] == []


def test_systematic_numeric_shift_is_real_and_collapsed():
    # R1-DATA-04 case 2: +0.001 on every row is a regression, not noise, and
    # must not print one line per row.
    old = [{"id": i, "score": i / 1000} for i in range(500)]
    new = [{"id": i, "score": i / 1000 + 0.001} for i in range(500)]
    diag = _diag({"scores": old}, {"scores": new})
    [f] = diag["behaviors"]["scores"]
    assert (f["path"], f["kind"], f["count"]) == ("$[*].score", REAL, 500)
    assert diag["suggestions"] == []


def test_order_only_list_change_is_real_first_then_sort():
    # R1-WEB-02 case 2 / R2-FIN-02 (D15): a reorder may be a set (sort it) or an
    # ordering regression (event application order): real change first.
    diag = _diag({"article": {"tags": ["python", "web", "api"]}},
                 {"article": {"tags": ["api", "python", "web"]}})
    [f] = diag["behaviors"]["article"]
    assert f["path"] == "tags" and f["kind"] == ORDER
    assert "looks like a real change" in f["note"] and "sort" in f["note"]
    assert "not part of the contract" in f["note"]
    assert diag["suggestions"] == []


def test_http_date_in_header_pairs_gets_no_global_rule():
    # R1-WEB-02 case 1 / R2-FIN-03 (D15): an unkeyed date gets capture-time advice,
    # never a global date pattern (masking "headers" would hide CSP/HSTS changes).
    def resp(date):
        return {"status": 200, "headers": [["Content-Type", "application/json"],
                                           ["Date", date],
                                           ["Content-Security-Policy", "default-src 'self'"]]}
    old, new = resp("Mon, 05 Oct 2026 12:00:00 GMT"), resp("Mon, 05 Oct 2026 12:00:02 GMT")
    diag = _diag({"GET.health": old}, {"GET.health": new})
    assert diag["suggestions"] == []
    [f] = diag["behaviors"]["GET.health"]
    assert f["kind"] == DATE and "dict" in f["note"]


def test_token_inside_html_body_gets_a_substring_rule():
    # R1-WEB-02 case 3: a CSRF token embedded in a rendered page.
    page = '<form><input type="hidden" name="csrf" value="{}"><p>Total: $5</p></form>'
    old = {"body": page.format("a8f3k29dk3m4n5b6v7c8x9z0")}
    new = {"body": page.format("q1w2e3r4t5y6u7i8o9p0a1s2")}
    diag = _diag({"GET.checkout": old}, {"GET.checkout": new})
    [s] = diag["suggestions"]
    assert "register_field" not in s["rule"]
    assert _apply(s["rule"], old) == _apply(s["rule"], new)
    changed_page = {"body": page.format("a8f3k29dk3m4n5b6v7c8x9z0").replace("$5", "$6")}
    assert _apply(s["rule"], changed_page) != _apply(s["rule"], old)


def test_raw_completion_envelope_never_suggests_masking_id():
    # R1-LLM-09: register_field("id") also blinded the stable doc ids in rag.hits.
    def completion(cid, created, fp, call):
        return {"id": f"chatcmpl-{cid}", "created": created, "model": "gpt-4o-mini",
                "system_fingerprint": f"fp_{fp}",
                "choices": [{"message": {"tool_calls": [{"id": f"call_{call}",
                                                         "function": {"name": "lookup"}}]}}]}
    old = completion("0a1b2c3d4e5f6a7b8c9d0e1f", 1790000000, "3c4d5e6f7a", "1a2b3c4d5e6f7a8b")
    new = completion("9f8e7d6c5b4a3f2e1d0c9b8a", 1790000417, "8b7a6f5e4d", "0f9e8d7c6b5a4f3e")
    hits = [{"id": "kb-refund-policy", "score": 1.37}, {"id": "kb-cancel-order", "score": 1.17}]
    diag = _diag({"llm.raw": old}, {"llm.raw": new}, **{"rag.hits": hits})
    rules = [s["rule"] for s in diag["suggestions"]]
    assert not any(re.search(r'register_field\("id"', r) for r in rules)
    assert 'scrub.register_field("created")' in rules
    for rule in rules:  # every suggested rule leaves the stable rag ids alone
        assert _apply(rule, hits) == hits
    tamed_old, tamed_new = old, new
    for rule in rules:
        tamed_old, tamed_new = _apply(rule, tamed_old), _apply(rule, tamed_new)
    assert tamed_old == tamed_new


def test_field_rule_withheld_when_the_key_is_stable_elsewhere():
    # R1-LLM-09: the same key, meaningful in another behavior.
    diag = _diag({"ev": {"created": 1790000000}}, {"ev": {"created": 1790000999}},
                 **{"invoice": {"created": 1700000000, "total": 5}})
    assert diag["suggestions"] == []
    [f] = diag["behaviors"]["ev"]
    assert f["kind"] == DATE and "invoice" in f["note"]


def test_adapter_content_hash_is_never_suggested():
    # R1-OPS-04: doctor told the user to mask text_sha256 project-wide.
    diag = _diag({"nginx.conf": {"encoding": "utf-8", "chars": 57, "lines": 4,
                                 "text_sha256": "97db198d" + "0" * 56}},
                 {"nginx.conf": {"encoding": "utf-8", "chars": 53, "lines": 4,
                                 "text_sha256": "28ee203b" + "1" * 56}})
    assert diag["suggestions"] == []
    kinds = _kinds(diag["behaviors"]["nginx.conf"])
    assert set(kinds) == {"chars", "text_sha256"}
    assert VOLATILE not in kinds.values()


def test_diagnose_ignores_new_and_removed():
    diag = diagnose({"gone": _b("gone", 1)}, {"fresh": _b("fresh", 2)})
    assert diag["changed"] == []
    assert diag["suggestions"] == []


def test_diagnose_root_change_yields_no_suggestion():
    diag = diagnose({"greeting": _b("greeting", "hi")}, {"greeting": _b("greeting", "yo")})
    assert _kinds(diag["behaviors"]["greeting"]) == {"$": REAL}
    assert diag["suggestions"] == []


def test_suggested_rules_are_valid_against_canonical_json():
    # Every suggested regex keeps the payload valid JSON (it stays inside a string).
    old = {"at": "Mon, 05 Oct 2026 12:00:00 GMT", "id": "req_4f5a6b7c8d9e0f1a"}
    new = {"at": "Tue, 06 Oct 2026 08:30:59 GMT", "id": "req_0a9b8c7d6e5f4a3b"}
    diag = _diag({"r": old}, {"r": new})
    assert len(diag["suggestions"]) == 2
    for s in diag["suggestions"]:
        assert canonical_json(_apply(s["rule"], new))


def test_cli_doctor_says_real_change_and_suggests_nothing(tmp_path):
    import subprocess
    import sys

    from nightward.core.baseline import Store
    store = Store(tmp_path / ".nw")
    store.ensure()
    store.write_pending(_b("reply", {"content": "Your refund is approved."}))
    store.approve("reply")
    store.write_pending(_b("reply", {"content": "환불이 승인되었습니다."}))
    r = subprocess.run([sys.executable, "-m", "nightward", "doctor", "--dir", str(store.root)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, r.stderr
    assert "looks like a real change" in r.stdout
    assert "register_field" not in r.stdout and "scrub.register" not in r.stdout


# --- D15: doctor never widens a mask beyond the evidence -----------------------


def _f32(x: float) -> float:
    import struct
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _next_f32(x: float) -> float:
    import struct
    bits = struct.unpack("<I", struct.pack("<f", x))[0]
    return struct.unpack("<f", struct.pack("<I", bits + 1))[0]


def test_event_order_change_prints_as_real_in_cli(tmp_path):
    # R2-FIN-02: an event-application-order regression was marked "~" noise.
    import subprocess
    import sys

    from nightward.core.baseline import Store
    store = Store(tmp_path / ".nw")
    store.ensure()
    store.write_pending(_b("webhook", {"applied": ["created", "succeeded", "refunded"]}))
    store.approve("webhook")
    store.write_pending(_b("webhook", {"applied": ["refunded", "created", "succeeded"]}))
    r = subprocess.run([sys.executable, "-m", "nightward", "doctor", "--dir", str(store.root)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "* applied" in r.stdout and "~ applied" not in r.stdout
    assert "look like real changes" in r.stdout


def test_db_datetime_rule_is_scoped_to_its_key(tmp_path):
    # R2-FIN-03: the global date-time regex also masked a stable statement period.
    period = {"start": "2026-01-01 00:00:00", "end": "2026-01-31 23:59:59"}
    diag = _diag({"audit": {"action": "refund", "logged_at": "2026-10-05 15:53:45.187003"}},
                 {"audit": {"action": "refund", "logged_at": "2026-10-05 15:53:47.390816"}},
                 **{"statement.period": period})
    [s] = diag["suggestions"]
    assert s["rule"] == 'scrub.register_field("logged_at")' and s["conditional"]
    assert _apply(s["rule"], period) == period
    [f] = diag["behaviors"]["audit"]
    assert f["kind"] == DATE and "looks like a real change" in f["note"]


def test_float_epoch_under_time_key_is_a_date_not_float_noise():
    # R2-FIN-03 note: time.time() under "at" was "float noise: round(value, 10)".
    diag = _diag({"e": {"at": 1791205923.5}}, {"e": {"at": 1791205924.25}})
    [f] = diag["behaviors"]["e"]
    assert f["kind"] == DATE


def test_sub_unit_change_on_a_large_float_is_real():
    # R2-DATA-01: +0.5 on 1.23e9 was "float noise in the last digits".
    diag = _diag({"gmv": {"gmv_krw": 1_234_567_890.0, "users": 51_744_876.0}},
                 {"gmv": {"gmv_krw": 1_234_567_890.5, "users": 51_744_876.04}})
    assert _kinds(diag["behaviors"]["gmv"]) == {"gmv_krw": REAL, "users": REAL}


def test_sign_of_zero_is_explained():
    # R2-DATA-01: 0.0 -> -0.0 is CHANGED in the gate, but doctor printed no path.
    [f] = findings({"residual": 0.0}, {"residual": -0.0})
    assert f["path"] == "residual" and "sign of zero" in f["note"]


def test_float32_one_ulp_drift_is_float_noise():
    # R2-LLM-03: cross-machine BLAS drift in float32 embeddings was "a real change".
    old = _f32(0.8137567043304443)
    diag = _diag({"emb": {"score": old}}, {"emb": {"score": _next_f32(old)}})
    [f] = diag["behaviors"]["emb"]
    assert f["kind"] == FLOAT and "6g" in f["note"]


def test_large_integral_floats_are_never_noise():
    # D15: a large absolute delta is never noise, even 1 float32 ulp apart.
    diag = _diag({"n": {"count": 16777216.0}}, {"n": {"count": 16777218.0}})
    assert _kinds(diag["behaviors"]["n"]) == {"count": REAL}


def test_scrub_false_behavior_gets_no_rule():
    # R2-DATA-02: the user opted out of scrubbing; a rule would not even apply.
    base = {"r": Behavior("r", {"id": "req_4f5a6b7c8d9e0f1a"}, scrub=False)}
    pend = {"r": Behavior("r", {"id": "req_0a9b8c7d6e5f4a3b"}, scrub=False)}
    diag = diagnose(base, pend)
    assert diag["suggestions"] == []
    assert "scrub=False" in diag["behaviors"]["r"][0]["note"]


def test_constant_date_shift_is_reported_as_real():
    # R2-DATA-02: every SLA deadline moved by exactly -24h after one code edit.
    diag = _diag({"sla": ["2024-01-05 09:15:00", "2024-01-05 23:50:00"]},
                 {"sla": ["2024-01-04 09:15:00", "2024-01-04 23:50:00"]})
    [f] = diag["behaviors"]["sla"]
    assert f["kind"] == REAL and "moved by -1 day" in f["note"] and f["count"] == 2
    assert diag["suggestions"] == []


def test_token_rule_has_open_length_and_base62_class():
    # R2-LLM-04: lengths 11..14 baked in -> a 10-char id flaked the gate; and two
    # hex samples gave [0-9a-f], which the first base62 id broke.
    diag = _diag({"c": {"trace": "req_4f5a6b7c8d9e0"}}, {"c": {"trace": "req_0a9b8c7d6e5"}})
    [s] = diag["suggestions"]
    tamed = _apply(s["rule"], {"trace": "req_4f5a6b7c8d9e0"})
    for later in ("req_Zz9yX8wV7u", "req_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"):
        assert _apply(s["rule"], {"trace": later}) == tamed


def test_token_rule_withheld_when_it_also_hits_stable_values():
    # D15: a pattern that would also match a stable value elsewhere is not offered.
    diag = _diag({"c": {"trace": "req_4f5a6b7c8d9e0f1a"}}, {"c": {"trace": "req_0a9b8c7d6e5f4a3b"}},
                 **{"fixture": {"pinned": "req_9z8y7x6w5v4u3t2s"}})
    assert diag["suggestions"] == []
    assert "fixture" in diag["behaviors"]["c"][0]["note"]


def test_string_epoch_header_is_recognized():
    # R2-WEB-04: X-RateLimit-Reset: "1791205923" was "a real change" with no remedy.
    diag = _diag({"api.429": {"headers": {"X-RateLimit-Reset": "1791205923"}}},
                 {"api.429": {"headers": {"X-RateLimit-Reset": "1791206121"}}})
    [s] = diag["suggestions"]
    assert s["rule"] == 'scrub.register_field("X-RateLimit-Reset")' and s["conditional"]
