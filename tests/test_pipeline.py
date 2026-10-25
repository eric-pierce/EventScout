from pathlib import Path

from fastapi.testclient import TestClient
from icalendar import Calendar

from eventscout.db import Database
from eventscout.filtering import hard_filter
from eventscout.models import RawEvent
from eventscout.pipeline import Service
from eventscout.web.app import create_app

from conftest import TZ, FakeLLM, at


def _src(db: Database, name: str):
    sid = db.add_source(name, f"https://{name}.example", "fake", "active", "manual")
    return db.get_source(sid)


def test_dedupe_merges_across_sources(db_config):
    db = Database(db_config, "America/New_York")
    a, b = _src(db, "a"), _src(db, "b")
    day = at(3).replace(hour=0)
    id1 = db.upsert_event(RawEvent(title="The Lowdown Brass Band", start=day, time_known=False, venue_name="Club Passim"), a)
    id2 = db.upsert_event(RawEvent(title="Lowdown Brass Band (live!)", start=at(3, 20), venue_name="Passim",
                                   price_min=20, url="https://x"), b)
    assert id1 == id2
    ev = db.get_event(id1)
    assert ev.time_known and ev.start.hour == 20 and ev.price_min == 20
    assert ev.source_names == ["a", "b"]
    # Same title at a different venue is a different event.
    id3 = db.upsert_event(RawEvent(title="Irish Session", start=at(3, 19), venue_name="The Druid"), a)
    id4 = db.upsert_event(RawEvent(title="Irish Session", start=at(3, 19), venue_name="The Burren"), a)
    assert id3 != id4


def test_hard_filter(cfg):
    db = Database(cfg.database, cfg.home.timezone)
    s = _src(db, "a")
    ev = db.get_event(db.upsert_event(RawEvent(title="Toddler Dance Party", start=at(2)), s))
    assert hard_filter(ev, cfg) == "excluded keyword: toddler"
    cfg.search.max_price = 50
    ev = db.get_event(db.upsert_event(RawEvent(title="Arena show", start=at(2), price_min=120), s))
    assert hard_filter(ev, cfg).startswith("price")
    db.update_event(ev.id, price_min=10, distance_miles=80.0)
    assert "mi away" in hard_filter(db.get_event(ev.id), cfg)


def test_full_run(cfg, fake_adapter):
    fake_adapter.events = [
        RawEvent(title="Late Night Jazz Trio", start=at(1, 21), venue_name="Wally's", city="Boston",
                 latitude=42.34, longitude=-71.08, price_min=10),
        # Weekly recurring listing: identical text -> scored once, cached for the other instance.
        RawEvent(title="Comedy Open Mic", start=at(2, 20), venue_name="Comedy Studio", is_free=True),
        RawEvent(title="Comedy Open Mic", start=at(9, 20), venue_name="Comedy Studio", is_free=True),
        RawEvent(title="Real Estate Seminar", start=at(4, 18)),
        RawEvent(title="Toddler Tunes", start=at(5, 10)),
        RawEvent(title="Way Later Jazz", start=at(60, 20)),  # outside horizon
    ]
    svc = Service(cfg)
    llm = FakeLLM()
    svc._llm = llm
    stats = svc.run()

    assert stats["fetch"]["unique_events"] == 5
    assert stats["filter"] == {"ok": 4, "filtered": 1}
    assert stats["score"]["scored"] == 4 and llm.calls.count("score") == 1
    assert stats["report"]["recommended"] == 3  # jazz + 2x comedy

    out = Path(cfg.report.output_dir)
    cal = Calendar.from_ical((out / "events.ics").read_bytes())
    titles = sorted(str(c.get("summary")) for c in cal.walk("VEVENT"))
    assert titles == ["Comedy Open Mic", "Comedy Open Mic", "Late Night Jazz Trio"]
    assert "Late Night Jazz Trio" in (out / "report.html").read_text()

    # Second run: nothing new to score.
    llm.calls.clear()
    stats = svc.run()
    assert "score" not in llm.calls
    assert stats["score"]["scored"] == 0


def test_web_app(cfg, fake_adapter):
    fake_adapter.events = [RawEvent(title="Jazz at the Lizard", start=at(1, 21), venue_name="Lizard Lounge")]
    cfg.report.feed_token = "sekret"
    svc = Service(cfg)
    svc._llm = FakeLLM()
    svc.run()
    client = TestClient(create_app(svc))

    r = client.get("/")
    assert r.status_code == 200 and "Jazz at the Lizard" in r.text
    ev_id = svc.db.upcoming_events(at(-1), at(5))[0].id

    assert client.post(f"/api/events/{ev_id}/star", json={"starred": True}).json() == {"ok": True}
    assert svc.db.get_event(ev_id).starred and svc.db.get_event(ev_id).feedback == 1
    assert client.post(f"/api/events/{ev_id}/feedback", json={"value": -1}).status_code == 200
    assert svc.db.feedback_examples()[0]["value"] == -1

    assert client.get("/calendar.ics").status_code == 401
    r = client.get("/calendar/starred.ics?token=sekret")
    assert r.status_code == 200 and b"Jazz at the Lizard" in r.content
    assert client.get("/sources").status_code == 200
    assert client.get("/runs").status_code == 200
    for view in ("all", "starred", "hidden", "filtered", "unscored"):
        assert client.get(f"/?view={view}").status_code == 200


def test_extraction_cache(cfg):
    from datetime import datetime

    from eventscout.tasks import extract_events

    db = Database(cfg.database, cfg.home.timezone)
    llm = FakeLLM(extract_events=[{
        "title": "Jazz Jam", "start": "2026-10-03T20:00", "end": None, "all_day": False, "venue_name": "Wally's",
        "address": None, "city": "Boston", "price_min": None, "price_max": None, "is_free": True, "cost_text": "free",
        "url": "/jam", "description": "", "categories": ["jazz"], "online": False,
    }])
    text = "Sat Oct 3 8pm Jazz Jam <https://club.example/jam> at Wally's, free\n" * 20
    for _ in range(2):
        [ev] = extract_events(llm, cfg, text, "https://club.example/cal", datetime(2026, 9, 27), db=db)
    assert llm.calls == ["extract"]  # second pass served from cache
    assert ev.url == "https://club.example/jam" and ev.start.hour == 20 and ev.is_free


def test_removed_seed_is_disabled(cfg, fake_adapter):
    from eventscout.config import SourceSeed

    Service(cfg)
    cfg.sources = [SourceSeed.model_construct(name="Fake2", url="https://fake.example/other", kind="fake", options={}, enabled=True)]
    svc = Service(cfg)
    status = {s.url: s.status for s in svc.db.list_sources()}
    assert status == {"https://fake.example/events": "disabled", "https://fake.example/other": "active"}


def test_dashboard_filters_accept_blank_and_junk_values(cfg, fake_adapter):
    fake_adapter.events = [
        RawEvent(title="Jazz Near", start=at(1, 21), latitude=42.34, longitude=-71.21),
        RawEvent(title="Jazz Far", start=at(1, 22), latitude=42.52, longitude=-70.89, is_free=True),
    ]
    svc = Service(cfg)
    svc._llm = FakeLLM()
    svc.run()
    client = TestClient(create_app(svc))
    # Exactly what the filter form submits when boxes are left empty.
    r = client.get("/?view=picks&q=&days=1&min_score=40&max_miles=")
    assert r.status_code == 200 and "Jazz Near" in r.text and "Jazz Far" in r.text
    r = client.get("/?view=picks&q=&days=&min_score=&max_miles=5&free=")
    assert r.status_code == 200 and "Jazz Near" in r.text and "Jazz Far" not in r.text
    r = client.get("/?days=abc&min_score=-3&max_miles=nan&free=true")
    assert r.status_code == 200 and "Jazz Far" in r.text and "Jazz Near" not in r.text  # free only


def test_dashboard_specific_date(cfg, fake_adapter):
    fake_adapter.events = [
        RawEvent(title="Jazz Tomorrow", start=at(1, 21)),
        RawEvent(title="Jazz In Three Days", start=at(3, 20)),
        RawEvent(title="Comedy In Three Days", start=at(3, 22), is_free=True),
    ]
    svc = Service(cfg)
    svc._llm = FakeLLM()
    svc.run()
    client = TestClient(create_app(svc))
    day3 = at(3).date().isoformat()

    r = client.get(f"/?view=picks&date={day3}&days=")
    assert r.status_code == 200
    assert "Jazz In Three Days" in r.text and "Comedy In Three Days" in r.text and "Jazz Tomorrow" not in r.text
    assert f'value="{day3}"' in r.text  # date box keeps the selection

    # A date overrides "days ahead"; other filters still apply.
    r = client.get(f"/?date={day3}&days=1&free=true")
    assert "Comedy In Three Days" in r.text and "Jazz In Three Days" not in r.text

    # Browse view honours the date too.
    assert "Jazz Tomorrow" in client.get(f"/?view=all&date={at(1).date().isoformat()}").text

    # Past dates snap to today; junk is ignored; beyond the horizon explains why it's empty.
    assert client.get("/?date=2001-01-01").status_code == 200
    r = client.get("/?date=not-a-date")
    assert r.status_code == 200 and "Jazz Tomorrow" in r.text
    r = client.get(f"/?date={at(cfg.search.horizon_days + 5).date().isoformat()}")
    assert "past the search window" in r.text


def test_email_digest_is_sent(cfg, fake_adapter, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def starttls(self, context=None):
            pass
        def login(self, user, password):
            pass
        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr("eventscout.report.mailer.smtplib.SMTP", FakeSMTP)
    cfg.email.enabled = True
    cfg.email.smtp_host, cfg.email.sender, cfg.email.recipients = "smtp.test", "me@test", ["me@test"]
    fake_adapter.events = [RawEvent(title="Jazz Night", start=at(1, 21))]
    svc = Service(cfg)
    svc._llm = FakeLLM()
    stats = svc.run()
    assert stats["report"]["emailed"] is True and len(sent) == 1
    msg = sent[0]
    assert "Jazz Night" in msg.get_body(("html",)).get_content()
    assert [p.get_filename() for p in msg.iter_attachments()] == ["eventscout.ics"]


def test_malformed_links_on_pages_do_not_crash(cfg):
    from eventscout.images import clean_image_url
    from eventscout.sources.jsonld import parse_jsonld_events
    from eventscout.tasks import html_to_text

    page = '<a href="http://[broken">Bad link</a> <a href="/ok">Good link</a>'
    text = html_to_text(page, "https://site.example/list")
    assert "Good link <https://site.example/ok>" in text and "Bad link" in text
    assert clean_image_url("http://[nope/x.png") is None
    ld = '<script type="application/ld+json">{"@type":"Event","name":"X","startDate":"2026-10-30","url":"http://[bad"}</script>'
    [ev] = parse_jsonld_events(ld, "https://site.example", TZ)
    assert ev.url is None


def test_one_bad_detail_page_does_not_sink_the_run(cfg, fake_adapter, monkeypatch):
    fake_adapter.events = [RawEvent(title="Jazz Night", start=at(1, 21), url="https://odd.example/e", time_known=False)]
    cfg.report.enrich_per_run = 5

    def exploding_get(self, url, **kw):
        raise RuntimeError("something unexpected in a third-party page")

    monkeypatch.setattr("eventscout.http.Fetcher.get", exploding_get)
    svc = Service(cfg)
    svc._llm = FakeLLM()
    stats = svc.run()  # must not raise
    assert stats["report"]["recommended"] == 1


def test_score_explanation_maps_to_profile_interests():
    from eventscout.config import Interest, Profile
    from eventscout.tasks import _explanation

    profile = Profile(interests=[Interest(topic="live music in small venues (indie, folk, jazz)", weight=5),
                                 Interest(topic="comedy and improv shows", weight=4)])
    d = _explanation({
        "matched": ["Live music in small venues (indie, folk, jazz)", "comedy & improv shows", "underwater basket weaving"],
        "against": ["networking mixers and sales pitches", "", "x" * 300, "four", "five"],
        "factors": ["7pm Tuesday fits your weeknight rule"],
    }, profile)
    assert d["matched"] == ["live music in small venues (indie, folk, jazz)", "comedy and improv shows"]  # exact topics
    assert d["against"][0] == "networking mixers and sales pitches" and len(d["against"]) == 3
    assert len(d["against"][1]) == 90
    assert d["factors"] == ["7pm Tuesday fits your weeknight rule"]


def test_why_panel_shows_breakdown_and_survives_cache(cfg, fake_adapter):
    from eventscout.config import Interest

    cfg.profile.interests = [Interest(topic="jazz", weight=5), Interest(topic="comedy", weight=3)]
    fake_adapter.events = [
        RawEvent(title="Jazz at Wally's", start=at(1, 21), venue_name="Wally's"),
        RawEvent(title="Jazz at Wally's", start=at(8, 21), venue_name="Wally's"),  # same listing next week -> cache
    ]
    svc = Service(cfg)
    svc._llm = FakeLLM()
    svc.run()
    evs = svc.db.upcoming_events(at(-1), at(10))
    assert [e.score_detail["matched"] for e in evs] == [["jazz"], ["jazz"]]
    assert svc.db.get_cached_score(evs[0].score_key)[3]["factors"] == ["Saturday evening"]

    html = TestClient(create_app(svc)).get("/").text
    assert "Why 90?" in html and "Matches your interests" in html
    assert '<span class="chip good" title="Interest weight 5 of 5">jazz<b>5/5</b></span>' in html
    assert "Also considered" in html and "Saturday evening" in html and "/settings#profile" in html


def test_why_panel_for_scores_without_breakdown(cfg, fake_adapter):
    fake_adapter.events = [RawEvent(title="Jazz Night", start=at(1, 21))]
    svc = Service(cfg)
    svc._llm = FakeLLM()
    svc.run()
    ev = svc.db.upcoming_events(at(-1), at(5))[0]
    svc.db.update_event(ev.id, score_detail=None)  # a score recorded before breakdowns existed
    html = TestClient(create_app(svc)).get("/").text
    assert "Why 90?" in html and "eventscout rescore" in html


def test_map_view_groups_events_by_venue(cfg, fake_adapter):
    import json
    import re

    wallys = dict(venue_name="Wally's Cafe", latitude=42.3409, longitude=-71.0816)
    fake_adapter.events = [
        RawEvent(title="Jazz Jam", start=at(1, 20), **wallys),
        RawEvent(title="Late Jazz Set", start=at(1, 23), **wallys),                       # same pin as above
        RawEvent(title="Jazz </script><script>alert(1)</script>", start=at(2, 20),       # hostile title
                 venue_name="Lizard Lounge", latitude=42.3889, longitude=-71.1194),
        RawEvent(title="Jazz Online", start=at(2, 21), online=True),                       # online: never mapped
        RawEvent(title="Jazz Somewhere", start=at(3, 21), city="Boston"),                  # no coordinates
    ]
    cfg.search.include_online = True
    svc = Service(cfg)
    svc._llm = FakeLLM()
    svc.run()
    client = TestClient(create_app(svc))

    r = client.get("/map?view=all")
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text                     # tojson escapes it inside the data block
    data = json.loads(re.search(r'<script id="map-data" type="application/json">(.*?)</script>', r.text, re.S).group(1))
    places = {p["name"]: p for p in data["places"]}
    assert len(places["Wally's Cafe"]["events"]) == 2 and places["Wally's Cafe"]["best"] == 90
    assert places["Lizard Lounge"]["events"][0]["title"].startswith("Jazz </script>")   # intact after decoding
    assert "2 without an exact location not on the map" in r.text
    assert data["home"]["radius_miles"] == cfg.search.max_distance_miles
    assert 'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="' in r.text

    # Same filters as the list: a date narrows the pins; the List/Map switch keeps the filters.
    day1 = at(1).date().isoformat()
    d1 = client.get(f"/map?view=all&date={day1}").text
    assert "Lizard Lounge" not in d1 and "Wally" in d1
    assert f'href="/?view=all&amp;q=&amp;days=&amp;date={day1}' in d1
    assert f'href="/map?view=all&amp;q=&amp;days=&amp;date={day1}' in client.get(f"/?view=all&date={day1}").text
