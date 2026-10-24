"""bostonshows.org: a static live-music listing for Greater Boston (thousands of rows, so parsed directly)."""

from __future__ import annotations

import re
from datetime import datetime, time

from bs4 import BeautifulSoup

from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter
from eventscout.urls import safe_urljoin

_PRICE = re.compile(r"\$(\d+(?:\.\d+)?)")
_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])m", re.I)


def parse_bostonshows(page_html: str, base_url: str, ctx: Context) -> list[RawEvent]:
    soup = BeautifulSoup(page_html, "lxml")
    out: list[RawEvent] = []
    for day in soup.select("div.date-events[data-date]"):
        try:
            day_date = datetime.strptime(day["data-date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (ctx.window_start.date() <= day_date <= ctx.window_end.date()):
            continue
        for row in day.select("tr.event"):
            title_el = row.select_one(".event-title")
            if not title_el:
                continue
            link = title_el.find("a")
            title = title_el.get_text(" ", strip=True)
            start_txt = (row.select_one(".event-start") or {}).get_text(strip=True) if row.select_one(".event-start") else ""
            m = _TIME.search(start_txt)
            known = bool(m)
            t = time(0, 0)
            if m:
                hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
                t = time(hour, int(m.group(2) or 0))
            venue_el = row.select_one(".event-venue")
            neighborhood = None
            if venue_el:
                hood = re.search(r"\(([^)]+)\)", venue_el.get_text(" ", strip=True))
                neighborhood = hood.group(1) if hood else None
            info = (row.select_one(".event-info").get_text(" ", strip=True) if row.select_one(".event-info") else "")
            prices = [float(p) for p in _PRICE.findall(info)]
            is_free = True if re.search(r"\bfree\b", info, re.I) and not prices else None
            genre = row.get("data-genre")
            cats = ["live music"] + ([genre] if genre and genre != "other" else [])
            if row.get("data-outdoor") == "True":
                cats.append("outdoor")
            desc_bits = [f"Live music at {row.get('data-venue')}" + (f" ({neighborhood})" if neighborhood else "")]
            if info:
                desc_bits.append(info)
            if row.get("data-recurring") == "True":
                desc_bits.append("recurring")
            out.append(RawEvent(
                title=title[:300],
                start=datetime.combine(day_date, t, tzinfo=ctx.tz),
                time_known=known,
                all_day=False,
                description=". ".join(desc_bits),
                venue_name=row.get("data-venue") or None,
                city=row.get("data-city") or None,
                price_min=min(prices) if prices else None,
                price_max=max(prices) if prices else None,
                is_free=is_free,
                cost_text=info if (prices or is_free) else None,  # info also holds age limits
                url=(safe_urljoin(base_url, link.get("href")) if link else None) or base_url,
                categories=cats,
            ))
    return out


class BostonShowsAdapter(SourceAdapter):
    kind = "bostonshows"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        res = ctx.fetcher.get(source.url)
        return parse_bostonshows(res.text, res.url, ctx)
