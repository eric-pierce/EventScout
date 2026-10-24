"""Localist calendars (used by many universities, towns and museums) via their public JSON API."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from eventscout.dates import parse_when
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter

_PRICE = re.compile(r"\$\s?(\d+(?:\.\d{2})?)")


def localist_base(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


class LocalistAdapter(SourceAdapter):
    kind = "localist"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        base = localist_base(source.url)
        days = max(1, (ctx.window_end - ctx.window_start).days + 1)
        max_pages = int(source.options.get("max_pages", 10))
        out: list[RawEvent] = []
        page: int | None = 1
        while page and page <= max_pages:
            params = {"days": min(days, 370), "pp": 100, "page": page}
            if kw := source.options.get("keyword"):
                params["keyword"] = kw
            res = ctx.fetcher.get(f"{base}/api/2/events", params=params, check_robots=False)
            data = json.loads(res.content)
            for wrapper in data.get("events", []):
                out += self._instances(wrapper.get("event", {}), ctx)
            page = (data.get("page") or {}).get("next_page")
        return out

    def _instances(self, ev: dict, ctx: Context) -> list[RawEvent]:
        online = ev.get("experience") == "virtual"
        geo = ev.get("geo") or {}
        cost = ev.get("ticket_cost") or None
        prices = [float(p) for p in _PRICE.findall(cost or "")]
        cats = []
        for key in ("event_types", "event_topic", "event_audience"):
            for f in (ev.get("filters") or {}).get(key, []) or []:
                cats.append(str(f.get("name", "")).lower())
        cats += [str(t).lower() for t in ev.get("tags", [])[:3]]
        out = []
        for inst in ev.get("event_instances", []):
            inst = inst.get("event_instance", inst)
            parsed = parse_when(inst.get("start"), ctx.tz)
            if not parsed:
                continue
            start, known = parsed
            end = parse_when(inst.get("end"), ctx.tz)
            all_day = bool(inst.get("all_day"))
            out.append(RawEvent(
                title=ev.get("title", "")[:300],
                start=start,
                end=end[0] if end and end[0] > start else None,
                all_day=all_day,
                time_known=known and not all_day,
                description=(ev.get("description_text") or "")[:1500],
                venue_name=ev.get("location_name") or None,
                address=ev.get("address") or None,
                city=geo.get("city"),
                latitude=float(geo["latitude"]) if geo.get("latitude") else None,
                longitude=float(geo["longitude"]) if geo.get("longitude") else None,
                is_free=True if ev.get("free") else None,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                cost_text=cost,
                url=ev.get("localist_url") or ev.get("url"),
                image_url=ev.get("photo_url"),
                categories=list(dict.fromkeys(c for c in cats if c))[:6],
                online=online,
            ))
        return out
