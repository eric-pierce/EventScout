"""iCalendar output: subscribable feeds and per-event downloads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from icalendar import Calendar, Event as ICalEvent, vText

from eventscout.models import Event


def _describe(ev: Event, base_url: str) -> str:
    lines = []
    if ev.score is not None:
        lines.append(f"Match {ev.score}/100 — {ev.score_reason or ''}".strip(" —"))
    lines.append(f"Cost: {ev.cost_display}")
    if ev.distance_miles is not None:
        d = f"{ev.distance_miles:.1f} mi from home"
        if ev.drive_minutes:
            d += f" (~{ev.drive_minutes:.0f} min drive)"
        lines.append(d)
    if ev.description:
        lines.append("")
        lines.append(ev.description)
    if ev.url:
        lines.append("")
        lines.append(ev.url)
    lines.append(f"\nvia {', '.join(ev.source_names)} · EventScout {base_url.rstrip('/')}/#event-{ev.id}")
    return "\n".join(lines)


def build_calendar(events: list[Event], name: str, base_url: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//EventScout//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", name)
    cal.add("x-published-ttl", "PT6H")
    cal.add("refresh-interval;value=duration", "PT6H")
    stamp = datetime.now(timezone.utc)
    for ev in events:
        item = ICalEvent()
        item.add("uid", f"eventscout-{ev.fingerprint}@eventscout")
        item.add("dtstamp", stamp)
        item.add("summary", ev.title)
        if ev.time_known and not ev.all_day:
            item.add("dtstart", ev.start)
            item.add("dtend", ev.end or ev.start + timedelta(hours=2))
        else:
            item.add("dtstart", ev.start.date())
            end_day = (ev.end.date() if ev.end and ev.end.date() > ev.start.date() else ev.start.date()) + timedelta(days=1)
            item.add("dtend", end_day)
        loc = ", ".join(dict.fromkeys(p for p in (ev.venue_name, ev.address or ev.city) if p))
        if loc:
            item["location"] = vText(loc)
        if ev.latitude is not None and ev.longitude is not None:
            item.add("geo", (ev.latitude, ev.longitude))
        item.add("description", _describe(ev, base_url))
        if ev.url:
            item.add("url", ev.url)
        if ev.tags or ev.categories:
            item.add("categories", list(dict.fromkeys(ev.tags + ev.categories))[:6])
        cal.add_component(item)
    return cal.to_ical()
