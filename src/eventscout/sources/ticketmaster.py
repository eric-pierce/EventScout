"""Ticketmaster Discovery API (free key: https://developer.ticketmaster.com). Good coverage of concerts, sports, theatre."""

from __future__ import annotations

import json
from datetime import timezone

from eventscout.dates import parse_when
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter

API = "https://app.ticketmaster.com/discovery/v2/events.json"


class TicketmasterAdapter(SourceAdapter):
    kind = "ticketmaster"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        key = source.options.get("api_key")
        if not key:
            raise ValueError("ticketmaster source needs options.api_key (set TICKETMASTER_API_KEY)")
        home = ctx.cfg.home
        out: list[RawEvent] = []
        max_pages = int(source.options.get("max_pages", 5))
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        for page in range(max_pages):
            params = {
                "apikey": key,
                "latlong": f"{home.latitude},{home.longitude}",
                "radius": int(ctx.cfg.search.max_distance_miles),
                "unit": "miles",
                "size": 200,
                "page": page,
                "sort": "date,asc",
                "startDateTime": ctx.window_start.astimezone(timezone.utc).strftime(fmt),
                "endDateTime": ctx.window_end.astimezone(timezone.utc).strftime(fmt),
            }
            if seg := source.options.get("segment"):
                params["segmentName"] = seg
            payload = json.loads(ctx.fetcher.get(API, params=params, check_robots=False).content)
            for ev in (payload.get("_embedded") or {}).get("events", []):
                raw = self._convert(ev, ctx)
                if raw:
                    out.append(raw)
            pg = payload.get("page") or {}
            if pg.get("number", 0) + 1 >= pg.get("totalPages", 0):
                break
        return out

    def _convert(self, ev: dict, ctx: Context) -> RawEvent | None:
        start_info = (ev.get("dates") or {}).get("start") or {}
        when = start_info.get("dateTime") or start_info.get("localDate")
        parsed = parse_when(when, ctx.tz)
        if not parsed:
            return None
        start, known = parsed
        if start_info.get("timeTBA") or start_info.get("noSpecificTime"):
            known = False
        venue = ((ev.get("_embedded") or {}).get("venues") or [{}])[0]
        loc = venue.get("location") or {}
        addr = ", ".join(x for x in [(venue.get("address") or {}).get("line1"), (venue.get("city") or {}).get("name"),
                                     (venue.get("state") or {}).get("stateCode")] if x)
        prices = ev.get("priceRanges") or []
        pmin = min((p.get("min") for p in prices if p.get("min") is not None), default=None)
        pmax = max((p.get("max") for p in prices if p.get("max") is not None), default=None)
        cls = (ev.get("classifications") or [{}])[0]
        cats = [((cls.get(k) or {}).get("name") or "").lower() for k in ("segment", "genre", "subGenre")]
        images = sorted(ev.get("images") or [], key=lambda i: i.get("width", 0))
        return RawEvent(
            title=ev.get("name", "")[:300],
            start=start,
            time_known=known,
            all_day=False,
            description=(ev.get("info") or ev.get("pleaseNote") or "")[:1500],
            venue_name=venue.get("name"),
            address=addr or None,
            city=(venue.get("city") or {}).get("name"),
            latitude=float(loc["latitude"]) if loc.get("latitude") else None,
            longitude=float(loc["longitude"]) if loc.get("longitude") else None,
            price_min=pmin,
            price_max=pmax,
            currency=(prices[0].get("currency") if prices else None),
            url=ev.get("url"),
            image_url=images[len(images) // 2]["url"] if images else None,
            categories=[c for c in dict.fromkeys(cats) if c and c != "undefined"],
        )
