"""Generic web page: use embedded schema.org JSON-LD when present, otherwise have the LLM read the page."""

from __future__ import annotations

from eventscout.dates import now
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter
from eventscout.sources.jsonld import page_urls, parse_jsonld_events
from eventscout.tasks import extract_events, html_to_text


class WebPageAdapter(SourceAdapter):
    kind = "web"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        events: list[RawEvent] = []
        force_llm = bool(source.options.get("force_llm"))
        for url in page_urls(source):
            res = ctx.fetcher.get(url, render=bool(source.options.get("render")))
            structured = [] if force_llm else parse_jsonld_events(res.text, res.url, ctx.tz)
            if len(structured) >= 2:
                events += structured
                continue
            text = html_to_text(res.text, res.url) if res.is_html else res.text
            if len(text) < 200:
                ctx.log.warning("%s: page has almost no text (JS-rendered? try options.render with browserless)", url)
                continue
            events += extract_events(ctx.llm, ctx.cfg, text, res.url, now(ctx.tz), db=ctx.db)
        return events
