"""iCalendar (.ics) feeds, with recurring events expanded inside the search window."""

from __future__ import annotations

import html
import re
from datetime import date, datetime

import recurring_ical_events
from icalendar import Calendar

from eventscout.dates import parse_when
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter


def _clean(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", ", ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", re.sub(r"(,\s*)+", ", ", text)).strip(" ,")


def parse_ics(data: bytes | str, ctx: Context) -> list[RawEvent]:
    cal = Calendar.from_ical(data)
    out: list[RawEvent] = []
    for comp in recurring_ical_events.of(cal).between(ctx.window_start, ctx.window_end):
        title = _clean(comp.get("SUMMARY"))
        dtstart = comp.get("DTSTART")
        if not title or dtstart is None:
            continue
        start_val = dtstart.dt
        all_day = isinstance(start_val, date) and not isinstance(start_val, datetime)
        parsed = parse_when(start_val, ctx.tz)
        if not parsed:
            continue
        start, known = parsed
        end = None
        if comp.get("DTEND") is not None:
            e = parse_when(comp.get("DTEND").dt, ctx.tz)
            end = e[0] if e and e[0] > start else None
        location = _clean(comp.get("LOCATION")) or None
        desc = _clean(comp.get("DESCRIPTION"))
        url = str(comp.get("URL", "")).strip() or None
        image = None
        for prop in ("IMAGE", "ATTACH"):
            vals = comp.get(prop)
            for v in vals if isinstance(vals, list) else [vals] if vals is not None else []:
                fmt = str(getattr(v, "params", {}).get("FMTTYPE", ""))
                if prop == "IMAGE" or fmt.startswith("image/"):
                    image = image or str(v)
        cats = []
        if comp.get("CATEGORIES") is not None:
            raw = comp.get("CATEGORIES")
            for c in raw if isinstance(raw, list) else [raw]:
                cats += [str(x).lower() for x in getattr(c, "cats", [c])]
        venue, address = None, location
        if location and "," in location:
            venue, _, rest = location.partition(",")
            venue, address = venue.strip(), location
        out.append(RawEvent(
            title=title[:300], start=start, end=end, all_day=all_day, time_known=known and not all_day,
            description=desc[:1500], venue_name=venue or location, address=address, url=url, categories=cats[:6],
            image_url=image,
        ))
    return out


class ICalAdapter(SourceAdapter):
    kind = "ical"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        url = source.url.replace("webcal://", "https://", 1)
        res = ctx.fetcher.get(url)
        return parse_ics(res.content, ctx)
