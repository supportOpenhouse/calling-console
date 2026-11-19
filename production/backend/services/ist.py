"""IST helpers. Campaigns run on Indian time regardless of server TZ."""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(s: str) -> tuple[int, int]:
    m = _HHMM.match(s) if isinstance(s, str) else None
    if not m:
        raise ValueError(f"invalid time {s!r}, expected H:MM or HH:MM")
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        raise ValueError(f"time out of range: {s!r}")
    return h, mi


def _minutes(s: str) -> int:
    h, m = parse_hhmm(s)
    return h * 60 + m


def validate_window(start: str, end: str) -> None:
    if _minutes(start) >= _minutes(end):
        raise ValueError("window start must be before window end")


def _ist_minutes(t: datetime) -> int:
    ist = t.astimezone(IST)
    return ist.hour * 60 + ist.minute


def in_window(now: datetime, start: str, end: str) -> bool:
    return _minutes(start) <= _ist_minutes(now) < _minutes(end)


def next_window_start(now: datetime, start: str, end: str) -> datetime:
    if in_window(now, start, end):
        return now
    h, m = parse_hhmm(start)
    ist = now.astimezone(IST)
    day = ist.date() + timedelta(days=0 if _ist_minutes(now) < _minutes(start) else 1)
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST).astimezone(timezone.utc)


def minutes_left_in_window(t: datetime, start: str, end: str) -> int:
    if not in_window(t, start, end):
        return 0
    return _minutes(end) - _ist_minutes(t)


def fmt_ist(dt: datetime | None, seconds: bool = False) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(IST).strftime("%d %b, %I:%M:%S %p" if seconds else "%d %b, %I:%M %p")
