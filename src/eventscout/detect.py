"""Work out what kind of source a URL is, and test that it yields upcoming events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from eventscout.http import Fetcher, FetchError
from eventscout.models import RawEvent, Source
from eventscout.sources import ADAPTERS, Context
from eventscout.sources.jsonld import parse_jsonld_events
from eventscout.urls import safe_urljoin


@dataclass
class Detection:
    kind: str
    url: str
    note: str = ""


def detect_kind(url: str, fetcher: Fetcher, ctx: Context) -> Detection:
    host = urlparse(url).netloc.lower()
    if "bostonshows.org" in host:
        return Detection("bostonshows", url)
    if url.startswith("webcal://") or url.lower().endswith(".ics"):
        return Detection("ical", url)

    res = fetcher.get(url)
    body = res.text.lstrip()[:500]
    if "text/calendar" in res.content_type or body.startswith("BEGIN:VCALENDAR"):
        return Detection("ical", res.url)
    if "xml" in res.content_type or body.startswith("<?xml"):
        if "<rss" in body or "<feed" in body or "rss" in res.content_type or "atom" in res.content_type:
            return Detection("rss", res.url)

    if not res.is_html:
        return Detection("web", res.url)

    # Localist calendars expose a JSON API.
    if "localist" in res.text[:200_000].lower():
        base = f"{urlparse(res.url).scheme}://{urlparse(res.url).netloc}"
        try:
            probe = fetcher.get(f"{base}/api/2/events", params={"pp": 1}, check_robots=False)
            if "events" in json.loads(probe.content):
                return Detection("localist", base, "Localist API")
        except (FetchError, ValueError):
            pass

    jsonld = parse_jsonld_events(res.text, res.url, ctx.tz)
    if len(jsonld) >= 2:
        return Detection("web", res.url, f"{len(jsonld)} JSON-LD events on page")

    soup = BeautifulSoup(res.text, "lxml")
    for link in soup.find_all("link", rel="alternate", href=True):
        t = (link.get("type") or "").lower()
        if "calendar" in t:
            if ical := safe_urljoin(res.url, link["href"]):
                return Detection("ical", ical, "linked iCal feed")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("webcal://") or re.search(r"\.ics($|\?)", href) or "ical" in href.lower() and "export" in href.lower():
            full = safe_urljoin(res.url, href)
            if full and urlparse(full).netloc == urlparse(res.url).netloc:
                return Detection("ical", full, "linked iCal feed")
    return Detection("web", res.url, "LLM page extraction")


def trial_fetch(det: Detection, name: str, ctx: Context, options: dict | None = None) -> list[RawEvent]:
    from datetime import datetime, timezone

    probe = Source(id=0, name=name, url=det.url, kind=det.kind, status="trial", origin="discovered",
                   options=options or {}, added_at=datetime.now(timezone.utc))
    events = ADAPTERS[det.kind].fetch(probe, ctx)
    return [e for e in events if ctx.in_window(e)]
