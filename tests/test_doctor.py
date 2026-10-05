"""doctor calls a drift volatile only on evidence, suggests the narrowest rule
that hides exactly that drift, and never suggests scrubbing a real change."""
import re

from nightward import scrub
from nightward.core.behavior import Behavior, canonical_json
from nightward.core.doctor import REAL, STRUCTURAL, VOLATILE, diagnose, findings


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
    exec(rule, {"scrub": scrub})
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
                             "items[]": STRUCTURAL, "meta": STRUCTURAL}


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


def test_order_only_list_change_says_sort():
    # R1-WEB-02 case 2: a set-derived list reorders with PYTHONHASHSEED.
    diag = _diag({"article": {"tags": ["python", "web", "api"]}},
                 {"article": {"tags": ["api", "python", "web"]}})
    [f] = diag["behaviors"]["article"]
    assert f["path"] == "tags" and "sort" in f["note"]
    assert diag["suggestions"] == []


def test_http_date_in_header_pairs_gets_a_value_shaped_rule():
    # R1-WEB-02 case 1: masking "headers" would hide CSP/HSTS regressions.
    def resp(date):
        return {"status": 200, "headers": [["Content-Type", "application/json"],
                                           ["Date", date],
                                           ["Content-Security-Policy", "default-src 'self'"]]}
    old, new = resp("Mon, 05 Oct 2026 12:00:00 GMT"), resp("Mon, 05 Oct 2026 12:00:02 GMT")
    diag = _diag({"GET.health": old}, {"GET.health": new})
    [s] = diag["suggestions"]
    assert "register_field" not in s["rule"]
    assert _apply(s["rule"], old) == _apply(s["rule"], new)
    # the security headers stay gated
    no_csp = resp("Mon, 05 Oct 2026 12:00:00 GMT")
    no_csp["headers"].pop()
    assert _apply(s["rule"], no_csp) != _apply(s["rule"], old)


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
    assert f["kind"] == VOLATILE and "invoice" in f["note"]


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
