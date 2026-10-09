"""Scrub must neutralize volatile fields, or the tool dies of false positives."""
import pytest

from nightward.errors import NightwardError
from nightward.scrub import register, register_field, scrub


def test_timestamp_scrubbed():
    a = scrub({"at": "2026-06-05T12:00:00Z", "v": 1})
    b = scrub({"at": "2099-01-01T23:59:59Z", "v": 1})
    assert a == b
    assert a["at"] == "<TIMESTAMP>"


def test_uuid_scrubbed():
    a = scrub({"id": "12345678-1234-1234-1234-123456789abc"})
    b = scrub({"id": "ffffffff-ffff-ffff-ffff-ffffffffffff"})
    assert a == b == {"id": "<UUID>"}


def test_stable_value_untouched():
    assert scrub({"total": 28.05, "roles": ["member"]}) == {"total": 28.05, "roles": ["member"]}


def test_custom_scrubber():
    register(r"ord_\d+", "<ORDER>")
    assert scrub({"order": "ord_98213"}) == {"order": "<ORDER>"}


def test_non_serializable_raises():
    with pytest.raises(NightwardError):
        scrub({"bad": object()})


def test_field_scrubbed_at_any_depth():
    register_field("created_at")
    a = scrub({"created_at": "run-1", "items": [{"created_at": 123, "qty": 2}]})
    b = scrub({"created_at": "run-2", "items": [{"created_at": 456, "qty": 2}]})
    assert a == b
    assert a == {"created_at": "<SCRUBBED>", "items": [{"created_at": "<SCRUBBED>", "qty": 2}]}


def test_field_scrub_custom_replacement():
    register_field("attempts", 0)
    assert scrub({"attempts": 7, "ok": True}) == {"attempts": 0, "ok": True}


def test_field_scrub_leaves_lookalike_values_alone():
    # Unlike text-regex scrubbing, only the named field is masked: a value that
    # *looks* identical under a different key must survive untouched.
    register_field("seen_at", "<T>")
    out = scrub({"seen_at": "2026-06-10", "release_date": "2026-06-10"})
    assert out == {"seen_at": "<T>", "release_date": "2026-06-10"}


def test_field_scrub_runs_before_text_scrubbers():
    # Masking the field removes the timestamp before the regex pass would hit it.
    register_field("at")
    out = scrub({"at": "2026-06-05T12:00:00Z"})
    assert out == {"at": "<SCRUBBED>"}


# ---- R1-DATA-01: default scrubbing is opt-out-able and counted --------------

def test_scrub_counted_reports_default_masks():
    from nightward.scrub import scrub_counted
    value, masked = scrub_counted({"at": "2026-06-05T12:00:00Z",
                                   "id": "12345678-1234-1234-1234-123456789abc", "v": 1})
    assert value == {"at": "<TIMESTAMP>", "id": "<UUID>", "v": 1}
    assert masked == 2


def test_scrub_disabled_keeps_values_but_still_validates():
    from nightward.scrub import scrub_counted
    register_field("v", 0)
    assert scrub_counted({"at": "2026-06-05T12:00:00Z", "v": (1, 2)}, enabled=False) == (
        {"at": "2026-06-05T12:00:00Z", "v": [1, 2]}, 0)
    with pytest.raises(NightwardError):
        scrub_counted({"bad": object()}, enabled=False)


def test_disable_defaults_keeps_custom_scrubbers():
    from nightward.scrub import disable_defaults, scrub_counted
    disable_defaults()
    register(r'"ord_\d+"', '"<ORDER>"')
    assert scrub_counted({"at": "2026-06-05T12:00:00Z", "o": "ord_1"}) == (
        {"at": "2026-06-05T12:00:00Z", "o": "<ORDER>"}, 0)


def test_collapsed_keys_message_points_at_the_opt_out():
    with pytest.raises(NightwardError, match="scrub=False"):
        scrub({"2024-01-01T00:00:00": 1, "2024-01-02T00:00:00": 2})
