from datetime import datetime, timezone

import pytest

from backend.services.ist import (IST, fmt_ist, in_window, minutes_left_in_window,
                                  next_window_start, parse_hhmm, validate_window)


def ist(h, m, d=18):
    return datetime(2026, 11, d, h, m, tzinfo=IST)


@pytest.mark.parametrize("h,m,expected", [(9, 59, False), (10, 0, True), (18, 59, True), (19, 0, False)])
def test_in_window(h, m, expected):
    assert in_window(ist(h, m), "10:00", "19:00") is expected


def test_next_window_start_inside_returns_now():
    now = ist(12, 0)
    assert next_window_start(now, "10:00", "19:00") == now


def test_next_window_start_before_and_after():
    assert next_window_start(ist(8, 0), "10:00", "19:00") == ist(10, 0)
    got = next_window_start(ist(20, 0), "10:00", "19:00")
    assert got == ist(10, 0, d=19)
    assert got.utcoffset().total_seconds() == 0


def test_minutes_left():
    assert minutes_left_in_window(ist(18, 30), "10:00", "19:00") == 30


def test_validate_window():
    validate_window("10:00", "19:00")
    for s, e in [("19:00", "10:00"), ("25:00", "26:00"), ("10:00", "10:00")]:
        with pytest.raises(ValueError):
            validate_window(s, e)


@pytest.mark.parametrize("bad", ["", "10", "10:5", "24:00", "10:60", "ab:cd", None])
def test_parse_hhmm_rejects(bad):
    with pytest.raises(ValueError):
        parse_hhmm(bad)


def test_parse_hhmm_ok():
    assert parse_hhmm("9:05") == (9, 5)


def test_fmt_ist():
    assert fmt_ist(datetime(2026, 11, 18, 9, 35, tzinfo=timezone.utc)) == "18 Nov, 03:05 PM"
    assert fmt_ist(datetime(2026, 11, 18, 9, 35, 7, tzinfo=timezone.utc), seconds=True) == "18 Nov, 03:05:07 PM"
    assert fmt_ist(None) == "—"
