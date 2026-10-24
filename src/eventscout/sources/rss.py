"""RSS/Atom feeds. Event dates are rarely structured in RSS, so entries are handed to the LLM extractor."""

from __future__ import annotations

import feedparser

from eventscout.dates import now
from eventscout.models import RawEvent, Source
from eventscout.sources.base import Context, SourceAdapter
from eventscout.tasks import extract_events, html_to_text


class RSSAdapter(SourceAdapter):
    kind = "rss"

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        res = ctx.fetcher.get(source.url)
        feed = feedparser.parse(res.content)
        if not feed.entries:
            return []
        lines = []
        for e in feed.entries[:150]:
            summary = html_to_text(e.get("summary", "") or "", source.url)[:800]
            lines.append(f"## {e.get('title', '')}\nlink: {e.get('link', '')}\npublished: {e.get('published', '')}\n{summary}")
        return extract_events(ctx.llm, ctx.cfg, "\n\n".join(lines), source.url, now(ctx.tz), db=ctx.db)
