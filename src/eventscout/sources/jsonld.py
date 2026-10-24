"""schema.org Event extraction from JSON-LD blocks (Eventbrite, Meetup, AllEvents, many venue sites)."""

from __future__ import annotations

import html
import json
import re
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from eventscout.dates import parse_when
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter
from eventscout.urls import safe_urljoin

_EVENT_TYPES = re.compile(r"Event$|^(Festival|Exhibition|Concert|Performance)$", re.I)


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else list(t)


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        types = _types(node)
        if any(_EVENT_TYPES.search(t) for t in types) or (not types and "startDate" in node and "name" in node):
            yield node
            return
        for key in ("@graph", "itemListElement", "item", "mainEntity", "subEvent", "event", "events"):
            if key in node:
                yield from _walk(node[key])


def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else v


def _text(v: Any) -> str | None:
    v = _first(v)
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@id")
    s = html.unescape(str(v)).strip()
    return s or None


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def event_from_jsonld(node: dict, tz: ZoneInfo, base_url: str) -> RawEvent | None:
    title = _text(node.get("name"))
    parsed = parse_when(_first(node.get("startDate")), tz)
    if not title or not parsed:
        return None
    start, known = parsed
    end = parse_when(_first(node.get("endDate")), tz)
    end_dt = end[0] if end else None
    if end_dt and not end[1] and not known:
        end_dt = None  # date-only end on a date-only event: treat as single all-day event
    if end_dt and end_dt <= start:
        end_dt = None

    status = str(node.get("eventStatus", ""))
    if "Cancelled" in status or "Postponed" in status:
        return None

    loc = _first(node.get("location")) or {}
    venue = address = city = None
    lat = lon = None
    online = "Online" in str(node.get("eventAttendanceMode", "")) and "Mixed" not in str(node.get("eventAttendanceMode", ""))
    if isinstance(loc, dict):
        if "VirtualLocation" in _types(loc):
            online = True
        venue = _text(loc.get("name"))
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion"), addr.get("postalCode")]
            address = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip()) or None
            city = (addr.get("addressLocality") or "").strip() or None
        elif isinstance(addr, str):
            address = addr.strip() or None
        geo = loc.get("geo") or {}
        if isinstance(geo, dict):
            lat, lon = _num(geo.get("latitude")), _num(geo.get("longitude"))
    elif isinstance(loc, str):
        venue = loc

    price_min = price_max = None
    is_free = node.get("isAccessibleForFree")
    is_free = bool(is_free) if isinstance(is_free, bool) else None
    currency = None
    offers = node.get("offers")
    offers = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
    prices = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        currency = currency or o.get("priceCurrency")
        for key in ("price", "lowPrice", "highPrice"):
            p = _num(o.get(key))
            if p is not None:
                prices.append(p)
    if prices:
        price_min, price_max = min(prices), max(prices)
        if price_max == 0:
            is_free = True

    url = _text(node.get("url")) or _text(node.get("@id"))
    image = _first(node.get("image"))
    if isinstance(image, dict):
        image = image.get("url")
    desc = _text(node.get("description")) or ""
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()

    return RawEvent(
        title=title[:300],
        start=start,
        end=end_dt,
        time_known=known,
        all_day=False,
        description=desc[:1500],
        venue_name=venue,
        address=address,
        city=city,
        latitude=lat,
        longitude=lon,
        price_min=price_min,
        price_max=price_max,
        is_free=is_free,
        currency=currency,
        url=safe_urljoin(base_url, url),
        image_url=safe_urljoin(base_url, image) if isinstance(image, str) else None,
        online=online,
    )


def parse_jsonld_events(page_html: str, base_url: str, tz: ZoneInfo) -> list[RawEvent]:
    soup = BeautifulSoup(page_html, "lxml")
    out: list[RawEvent] = []
    for tag in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:  # some sites emit stray control characters
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except json.JSONDecodeError:
                continue
        for node in _walk(data):
            ev = event_from_jsonld(node, tz, base_url)
            if ev:
                out.append(ev)
    return out


def page_urls(source: Source) -> list[str]:
    """A source can list extra pages via options.extra_urls, or paginate with options.pages + options.page_param."""
    urls = [source.url]
    opts = source.options
    pages = int(opts.get("pages", 1))
    if pages > 1:
        param = opts.get("page_param", "page")
        sep = "&" if "?" in source.url else "?"
        urls += [f"{source.url}{sep}{param}={n}" for n in range(2, pages + 1)]
    urls += list(opts.get("extra_urls", []))
    return urls


class JsonLdAdapter(SourceAdapter):
    kind = "jsonld"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        events: list[RawEvent] = []
        for url in page_urls(source):
            res = ctx.fetcher.get(url, render=bool(source.options.get("render")))
            events += parse_jsonld_events(res.text, res.url, ctx.tz)
        return events
