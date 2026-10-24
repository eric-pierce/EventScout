"""Date parsing helpers. Everything is normalised to timezone-aware datetimes in the home timezone."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_when(value: object, tz: ZoneInfo) -> tuple[datetime, bool] | None:
    """Parse a date/datetime-ish value.

    Returns (aware datetime in `tz`, time_known). Date-only values become midnight with time_known=False.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt, known = value, True
    elif isinstance(value, date):
        return datetime.combine(value, time(0, 0), tzinfo=tz), False
    else:
        text = str(value).strip()
        if _DATE_ONLY.match(text):
            d = date.fromisoformat(text)
            return datetime.combine(d, time(0, 0), tzinfo=tz), False
        try:
            dt = dateparser.parse(text)
        except (ValueError, OverflowError):
            return None
        if dt is None:
            return None
        known = True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt, known


def now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)
