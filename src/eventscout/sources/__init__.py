from eventscout.sources.base import Context, SourceAdapter
from eventscout.sources.bostonshows import BostonShowsAdapter
from eventscout.sources.ical import ICalAdapter
from eventscout.sources.jsonld import JsonLdAdapter
from eventscout.sources.localist import LocalistAdapter
from eventscout.sources.rss import RSSAdapter
from eventscout.sources.ticketmaster import TicketmasterAdapter
from eventscout.sources.web import WebPageAdapter

ADAPTERS: dict[str, SourceAdapter] = {
    a.kind: a
    for a in (
        WebPageAdapter(),
        JsonLdAdapter(),
        ICalAdapter(),
        RSSAdapter(),
        LocalistAdapter(),
        TicketmasterAdapter(),
        BostonShowsAdapter(),
    )
}

__all__ = ["ADAPTERS", "Context", "SourceAdapter"]
