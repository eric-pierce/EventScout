import json
from datetime import datetime

from eventscout.sources.bostonshows import parse_bostonshows
from eventscout.sources.ical import parse_ics
from eventscout.sources.jsonld import parse_jsonld_events
from eventscout.tasks import chunk_text, html_to_text

from conftest import TZ


def _page(*blocks) -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body></body></html>"


def test_jsonld_itemlist_without_types_and_offers():
    page = _page({
        "@context": "https://schema.org",
        "itemListElement": [{
            "@type": "ListItem",
            "item": {
                "name": "Wicked Comic Con", "startDate": "2026-10-04", "endDate": "2026-10-04",
                "url": "/e/1", "location": {"name": "Westin", "address": {"streetAddress": "425 Summer St",
                                                                          "addressLocality": "Boston", "addressRegion": "MA"}},
                "offers": [{"price": "0", "priceCurrency": "USD"}],
            },
        }],
    })
    [ev] = parse_jsonld_events(page, "https://www.eventbrite.com/d/x", TZ)
    assert ev.title == "Wicked Comic Con"
    assert ev.time_known is False
    assert ev.is_free is True
    assert ev.city == "Boston"
    assert ev.url == "https://www.eventbrite.com/e/1"


def test_jsonld_utc_time_and_virtual_location():
    page = _page([{
        "@type": "Event", "name": "Bonus Parents Virtual Meetup", "startDate": "2026-10-07T21:30:00.000Z",
        "endDate": "", "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
        "location": {"@type": "VirtualLocation", "url": "https://meetup.com/x"},
    }])
    [ev] = parse_jsonld_events(page, "https://meetup.com", TZ)
    assert ev.online is True
    assert ev.start.hour == 17 and ev.start.tzinfo is not None  # 21:30Z -> 5:30pm EDT


def test_jsonld_graph_and_cancelled():
    page = _page({"@graph": [
        {"@type": "MusicEvent", "name": "Jazz Night", "startDate": "2026-10-10T20:00", "offers": {"lowPrice": 15, "highPrice": 25}},
        {"@type": "Event", "name": "Gone", "startDate": "2026-10-11T20:00", "eventStatus": "https://schema.org/EventCancelled"},
        {"@type": "Organization", "name": "Club"},
    ]})
    [ev] = parse_jsonld_events(page, "https://club.example", TZ)
    assert (ev.title, ev.price_min, ev.price_max) == ("Jazz Night", 15.0, 25.0)


def test_ical_recurring_and_html_location(ctx):
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Weekly Trivia\r\n"
        "DTSTART;TZID=America/New_York:20260901T190000\r\nDTEND;TZID=America/New_York:20260901T210000\r\n"
        "RRULE:FREQ=WEEKLY\r\nLOCATION:Peabody Museum &amp; Co<br>11 Divinity Ave<br>Cambridge\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events = parse_ics(ics.encode(), ctx)
    assert len(events) == 3  # Oct 1, 8, 15 inside the 21-day window starting Oct 1 09:00
    assert events[0].venue_name == "Peabody Museum & Co"
    assert "11 Divinity Ave" in events[0].address


BOSTONSHOWS = """
<div class="date-events" data-date="2026-10-02">
  <table>
  <tr class="event" data-city="Cambridge" data-venue="Middle East" data-genre="other" data-recurring="False" data-outdoor="False">
    <td class="event-start">8:30pm</td>
    <td class="event-details">
      <div class="event-title"><a href="https://tix.example/1">Band A, Band B</a></div>
      <div class="event-venue">at <a href="venues/me.html">Middle East</a> (Central Square)</div>
      <div class="event-info">$15–$20 / 18+</div>
    </td>
  </tr>
  <tr class="event" data-city="Somerville" data-venue="The Burren" data-genre="folk" data-recurring="True" data-outdoor="False">
    <td class="event-start"></td>
    <td class="event-details"><div class="event-title">Irish Session</div><div class="event-info">free</div></td>
  </tr>
  </table>
</div>
<div class="date-events" data-date="2027-01-01"><table><tr class="event"><td class="event-details"><div class="event-title">Too far</div></td></tr></table></div>
"""


def test_bostonshows(ctx):
    a, b = parse_bostonshows(BOSTONSHOWS, "https://bostonshows.org/", ctx)
    assert a.start == datetime(2026, 10, 2, 20, 30, tzinfo=TZ)
    assert (a.price_min, a.price_max, a.city) == (15.0, 20.0, "Cambridge")
    assert "Central Square" in a.description
    assert b.is_free is True and b.time_known is False and "folk" in b.categories


def test_html_to_text_keeps_links_and_chunks():
    text = html_to_text('<nav>menu</nav><div><a href="/e/5">Show</a> Oct 3</div><script>x()</script>', "https://s.example/list")
    assert "Show <https://s.example/e/5>" in text and "menu" not in text and "x()" not in text
    chunks = chunk_text("line\n" * 3000, 1000)
    assert all(len(c) <= 1000 for c in chunks) and "".join(chunks) == "line\n" * 3000


def test_attach_event_links_fixes_listing_and_madeup_urls():
    from eventscout.models import RawEvent
    from eventscout.tasks import attach_event_links

    page = "https://do617.com/events/film-theatre-performing-arts/today"
    text = (
        "Boston <https://do617.com/venues/boston>\n"  # venue link whose label is a subset of a title
        "Affordable Art Fair Boston <https://do617.com/events/2026/10/22/affordable-art-fair-boston-tickets>\n"
        "Rachel Bochner | The \u201cSorry If It\u2019s Selfish\u201d Tour <https://do617.com/events/2026/10/24/bochner>\n"
        "Big D and the Kids Table, The Pietasters, Devon K... <https://do617.com/events/2026/10/24/big-d-tickets>\n"
        "Operation Mincemeat: A New Musical <https://do617.com/events/2026/10/24/mincemeat-2pm>\n"
        "Operation Mincemeat: A New Musical <https://do617.com/events/2026/10/24/mincemeat-8pm>\n"
        "Comedy <https://do617.com/events/comedy/today>\n"
    )
    day = datetime(2026, 10, 24, tzinfo=TZ)
    evs = [
        RawEvent(title="Affordable Art Fair Boston", start=day.replace(hour=17), url=page),                 # listing page
        RawEvent(title="Big D and the Kids Table, The Pietasters, Devon Kay and the Solutions",
                 start=day.replace(hour=18), url="https://do617.com/made-up"),                                # not on page
        RawEvent(title="Operation Mincemeat: A New Musical", start=day.replace(hour=20), url=page),
        RawEvent(title="Operation Mincemeat: A New Musical", start=day.replace(hour=14), url=page),
        RawEvent(title="Some Unlisted Thing", start=day.replace(hour=21), url=None),
        RawEvent(title='Rachel Bochner - The "Sorry If It\'s Selfish" Tour', start=day.replace(hour=19), url=page),
        RawEvent(title="Boston Harbor Cruise", start=day.replace(hour=12), url=page),
    ]
    attach_event_links(evs, text, page)
    assert evs[0].url.endswith("affordable-art-fair-boston-tickets")
    assert evs[1].url.endswith("big-d-tickets")
    assert evs[3].url.endswith("mincemeat-2pm") and evs[2].url.endswith("mincemeat-8pm")  # start-time order
    assert evs[4].url == page
    assert evs[5].url.endswith("/bochner")   # curly vs straight quotes, | vs -
    assert evs[6].url == page                # "Boston" venue link must not match


def test_attach_event_links_uses_showtime_when_one_of_several_listings_is_extracted():
    from eventscout.models import RawEvent
    from eventscout.tasks import attach_event_links

    page = "https://do617.com/events/film-theatre-performing-arts/today"
    text = (
        "Operation Mincemeat: A New Musical <https://do617.com/e/mincemeat-a>\n"
        " Emerson Colonial Theatre <https://do617.com/venues/emerson-colonial-theatre>\n 2:00PM\n"
        "Operation Mincemeat: A New Musical <https://do617.com/e/mincemeat-b>\n"
        " Emerson Colonial Theatre <https://do617.com/venues/emerson-colonial-theatre>\n 8:00PM\n"
    )
    [ev] = evs = [RawEvent(title="Operation Mincemeat: A New Musical", start=datetime(2026, 10, 24, 20, tzinfo=TZ), url=page)]
    attach_event_links(evs, text, page)
    assert ev.url.endswith("mincemeat-b")
