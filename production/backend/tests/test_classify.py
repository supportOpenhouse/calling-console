import pytest

from backend.services.classify import (classify_execution, extract_tag, flatten_extracted,
                                       is_terminal_status, lead_status_for, normalize_phone)
from backend.tests.factories import bolna_execution

CASES = [
    (dict(status="balance-low"), "BALANCE_LOW"),
    (dict(telephony_data={"hangup_provider_code": 3010}), "CARRIER_BLOCKED"),
    (dict(telephony_data={"hangup_provider_code": 4010}), "VOICEMAIL"),
    (dict(telephony_data={"hangup_reason": "answering machine detected"}), "VOICEMAIL"),
    (dict(telephony_data={"hangup_provider_code": 3080}), "FAILED"),
    (dict(status="no-answer", transcript=None), "NOT_PICKED"),
    (dict(telephony_data={"hangup_reason": "x 3040 y"}, transcript=None), "NOT_PICKED"),
    (dict(transcript="assistant: hello\nuser: haan ji kaun", conversation_duration=40), "CONNECTED"),
    (dict(transcript="assistant: hello sir", conversation_duration=30, extracted_data=None), "VOICEMAIL"),
    (dict(transcript="assistant: hello\nuser: ha", conversation_duration=12, extracted_data=None), "NOT_PICKED"),
    (dict(transcript="assistant: aap ghar bechna chahte hain kya\nuser: aap ghar bechna chahte hain kya",
          conversation_duration=10, extracted_data=None), "NOT_PICKED"),  # echo
]


@pytest.mark.parametrize("overrides,expected", CASES)
def test_classify_outcomes(overrides, expected):
    assert classify_execution(bolna_execution(**overrides))["outcome"] == expected


def test_classify_fields():
    c = classify_execution(bolna_execution())
    assert c["hangup_code"] == 4000 and c["duration_sec"] == 60 and c["human_turns"] == 1
    assert c["tag"] == "qualified" and c["phone"] == "+919999900001"
    assert c["started_at"].tzinfo is not None and c["cost"] == 4.1


def test_terminal_status():
    assert is_terminal_status("Completed") and is_terminal_status("cancelled")
    assert not is_terminal_status("in-progress") and not is_terminal_status(None)


def test_extract_tag_nested_objective():
    assert extract_tag({"Seller Details": {"call_tag": {"objective": "qualified"}}}) == "qualified"
    assert extract_tag(None) is None


def test_flatten_extracted():
    assert flatten_extracted({"G": {"ask_price": {"objective": "1.5 Cr"}}}) == {"ask_price": "1.5 Cr"}


def test_lead_status_for():
    assert lead_status_for("CONNECTED", "qualified") == "DONE"
    assert lead_status_for("CONNECTED", "callback_requested") == "CONNECTED"
    assert lead_status_for("CONNECTED", "angry") == "DNC"
    assert lead_status_for("BALANCE_LOW", None) == "PENDING"
    assert lead_status_for("CARRIER_BLOCKED", None) == "BLOCKED"
    assert lead_status_for("NOT_PICKED", None) == "NOT_CONNECTED"


def test_normalize_phone():
    for raw in ["9810043058", "+91 98100 43058", "09810043058", "919810043058", 9810043058, 9810043058.0, "9810043058.0"]:
        assert normalize_phone(raw) == "+919810043058"
    for raw in ["9.81E+09", "12345", "", None, "abc"]:
        assert normalize_phone(raw) is None


def test_started_at_is_utc_aware():
    from datetime import datetime, timezone
    naive = classify_execution(bolna_execution(created_at="2026-11-11T08:03:24.123456"))["started_at"]
    assert naive == datetime(2026, 11, 11, 8, 3, 24, 123456, tzinfo=timezone.utc)
    off = classify_execution(bolna_execution(created_at="2026-11-11T13:33:24+05:30"))["started_at"]
    assert off == datetime(2026, 11, 11, 8, 3, 24, tzinfo=timezone.utc)
    assert off.utcoffset().total_seconds() == 0


def test_extract_tag_inside_list():
    assert extract_tag({"calls": [{"x": 1}, {"call_tag": {"objective": "booked"}}]}) == "booked"


def test_flatten_extracted_js_stringify():
    out = flatten_extracted({"G": {"a": True, "b": False, "c": 1.0, "d": None, "e": {"objective": {"k": 1}}}, "H": 2.0})
    assert out == {"a": "true", "b": "false", "c": "1", "d": "", "e": '{"k":1}', "H": "2"}
