"""Generic web page: use embedded schema.org JSON-LD when present, otherwise have the LLM read the page.

options.follow_links turns the page into an index: instead of reading the page itself, read the newest pages it links
to (e.g. a newsletter homepage whose events live in weekly posts):

    options:
      follow_links:
        match: "/p/things-to-do"   # regex the link URL must match
        max: 2                     # how many of the newest matching links to read
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

from eventscout.dates import now
from eventscout.http import FetchResult
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter
from eventscout.sources.jsonld import page_urls, parse_jsonld_events
from eventscout.tasks import extract_events, html_to_text
from eventscout.urls import safe_urljoin

# "Oct 16, 2026", "October 16th, 2026", "16 Oct 2026", "2026-10-16"
_LABEL_DATE = re.compile(
    r"\b(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})\b",
    re.I,
)


def label_date(text: str) -> date | None:
    """The publication date shown in a link's text, if any."""
    m = _LABEL_DATE.search(text)
    if not m:
        return None
    try:
        return dateparser.parse(re.sub(r"(\d)(st|nd|rd|th)", r"\1", m.group(0))).date()
    except (ValueError, OverflowError):
        return None


def follow_targets(html: str, base_url: str, spec: dict | str) -> list[str]:
    """Links on an index page to read instead of the page itself: same site, URL matching `match`, newest first
    (by a date in the link text; links without one keep page order after the dated ones), at most `max`."""
    if isinstance(spec, str):
        spec = {"match": spec}
    pattern = re.compile(spec.get("match") or ".")
    limit = int(spec.get("max", 2))
    host = urlparse(base_url).netloc
    found: dict[str, tuple[date | None, int]] = {}
    for i, a in enumerate(BeautifulSoup(html, "lxml").find_all("a", href=True)):
        url = safe_urljoin(base_url, a["href"])
        if not url:
            continue
        url = url.split("#", 1)[0]
        if urlparse(url).netloc != host or not pattern.search(url) or url.rstrip("/") == base_url.rstrip("/"):
            continue
        when = label_date(a.get_text(" ", strip=True))
        prev = found.get(url)
        if prev is None or (prev[0] is None and when is not None):  # the same post is often linked several times
            found[url] = (when, prev[1] if prev else i)
    ordered = sorted(found.items(), key=lambda kv: (kv[1][0] is None, -(kv[1][0] or date.min).toordinal(), kv[1][1]))
    return [url for url, _ in ordered[:limit]]


class WebPageAdapter(SourceAdapter):
    kind = "web"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        events: list[RawEvent] = []
        render = bool(source.options.get("render"))
        follow = source.options.get("follow_links")
        for url in page_urls(source):
            res = ctx.fetcher.get(url, render=render)
            if not follow:
                events += self._read(res, source, ctx)
                continue
            targets = follow_targets(res.text, res.url, follow)
            if not targets:
                ctx.log.warning("%s: follow_links matched no links on %s", source.name, res.url)
            for target in targets:
                events += self._read(ctx.fetcher.get(target, render=render), source, ctx)
        return events

    @staticmethod
    def _read(res: FetchResult, source: Source, ctx: Context) -> list[RawEvent]:
        structured = [] if source.options.get("force_llm") else parse_jsonld_events(res.text, res.url, ctx.tz)
        if len(structured) >= 2:
            return structured
        text = html_to_text(res.text, res.url) if res.is_html else res.text
        if len(text) < 200:
            ctx.log.warning("%s: page has almost no text (JS-rendered? try options.render with browserless)", res.url)
            return []
        return extract_events(ctx.llm, ctx.cfg, text, res.url, now(ctx.tz), db=ctx.db)
