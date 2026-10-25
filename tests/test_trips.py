import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from eventscout.config import SourceSeed
from eventscout.models import RawEvent
from eventscout.pipeline import Service
from eventscout.trips import Place, TripError, build_trip, builtin_sources, trip_config, trip_events, validate_dates
from eventscout.web.app import create_app

from conftest import FakeLLM, at

CHI = ZoneInfo("America/Chicago")
CHICAGO = Place(name="Chicago", label="Chicago, Illinois, United States", latitude=41.876, longitude=-87.624,
                timezone="America/Chicago", region="Illinois", region_code="US-IL", country="United States",
                country_code="us")


def chi(days: int, hour: int) -> datetime:
    return (datetime.now(CHI) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def make_trip(svc, start_in=3, nights=2, discover=False, notes="No car; free all day"):
    start = datetime.now(CHI).date() + timedelta(days=start_in)
    return svc.db.get_trip(svc.db.create_trip(
        name=CHICAGO.label, query="Chicago, IL", latitude=CHICAGO.latitude, longitude=CHICAGO.longitude,
        timezone=CHICAGO.timezone, city="Chicago", region="Illinois", region_code="US-IL", country="United States",
        country_code="us", start_date=start, end_date=start + timedelta(days=nights), radius_miles=8,
        notes=notes, discover=discover,
    ))


@pytest.fixture
def trip_sources(monkeypatch, fake_adapter):
    """Trip builds use the fake adapter instead of the real city aggregators."""
    monkeypatch.setattr("eventscout.trips.builtin_sources",
                        lambda trip, cfg: [{"builtin": "fakebrite", "name": "Fake Chicago", "url": "https://chi.example/",
                                            "kind": "fake"}])
    monkeypatch.setattr("eventscout.web.app.builtin_sources",
                        lambda trip, cfg: [{"builtin": "fakebrite", "name": "Fake Chicago", "url": "https://chi.example/",
                                            "kind": "fake"}])
    fake_adapter.events = [
        RawEvent(title="Green Mill Jazz", start=chi(3, 20), venue_name="Green Mill"),
        RawEvent(title="Second City Comedy", start=chi(4, 19), venue_name="Second City"),
        RawEvent(title="Timeshare Seminar", start=chi(4, 10)),
        RawEvent(title="Jazz After The Trip", start=chi(20, 20)),  # outside the trip dates
    ]
    return fake_adapter


def test_trip_config_puts_the_destination_in_place_of_home(cfg):
    svc = Service(cfg)
    trip = make_trip(svc)
    t = trip_config(cfg, trip)
    assert (t.home.city, t.home.timezone, t.search.max_distance_miles) == ("Chicago", "America/Chicago", 8)
    assert t.search.horizon_days == 2 and t.search.max_drive_minutes is None and t.sources == []
    assert "visiting Chicago" in t.profile.notes and "No car; free all day" in t.profile.notes
    assert cfg.home.city != "Chicago" and "visiting" not in cfg.profile.notes  # the home config is untouched
    assert t.profile.interests == cfg.profile.interests


def test_builtin_sources_for_us_and_abroad(cfg):
    svc = Service(cfg)
    trip = make_trip(svc)
    urls = {s["name"]: s["url"] for s in builtin_sources(trip, cfg)}
    assert urls["Eventbrite — Chicago"] == (f"https://www.eventbrite.com/d/il--chicago/all-events/"
                                            f"?start_date={trip.start_date}&end_date={trip.end_date}")
    assert urls["AllEvents — Chicago"] == "https://allevents.in/chicago"
    assert "location=us--il--Chicago" in urls["Meetup — Chicago"]
    assert not any("Ticketmaster" in n for n in urls)  # no key configured

    lisbon = trip.model_copy(update={"city": "Lisboa", "country": "Portugal", "country_code": "pt", "region_code": "PT-11"})
    names = {s["name"]: s["url"] for s in builtin_sources(lisbon, cfg)}
    assert "/d/portugal--lisboa/all-events/" in names["Eventbrite — Lisboa"] and "Meetup — Lisboa" not in names

    cfg.sources.append(SourceSeed(name="TM", url="https://app.ticketmaster.com/discovery/v2/events.json",
                                  kind="ticketmaster", options={"api_key": "k"}, enabled=False))
    assert any(s["kind"] == "ticketmaster" for s in builtin_sources(trip, cfg))


def test_validate_dates():
    today = date(2026, 10, 24)
    validate_dates(today, today + timedelta(days=2), today)
    for start, end, msg in [(today, today - timedelta(days=1), "ends before"),
                            (today - timedelta(days=5), today - timedelta(days=2), "already over"),
                            (today, today + timedelta(days=40), "at most")]:
        with pytest.raises(TripError, match=msg):
            validate_dates(start, end, today)


def test_build_trip_keeps_trip_events_apart_from_home(cfg, trip_sources):
    trip_sources.events = trip_sources.events + []  # same events serve the home run below
    svc = Service(cfg)
    svc._llm = llm = FakeLLM()
    trip = make_trip(svc)
    stats = build_trip(svc, trip.id)

    trip = svc.db.get_trip(trip.id)
    assert trip.status == "done" and stats["picks"] == 2  # jazz + comedy; the seminar scores low
    assert [s["name"] for s in trip.sources] == ["Fake Chicago"] and trip.sources[0]["events"] == 3
    assert any("visiting Chicago" in s for s in llm.systems)  # scored with the trip-aware profile

    events = trip_events(svc.db, trip)
    assert {e.title for e in events} == {"Green Mill Jazz", "Second City Comedy", "Timeshare Seminar"}
    assert all(e.trip_id == trip.id for e in events)
    assert next(e for e in events if e.title == "Green Mill Jazz").start.strftime("%H:%M") == "20:00"  # Chicago time

    # None of it reaches the home side...
    assert svc.db.upcoming_events(at(-1), at(30)) == []
    # ...and a home run with the same listings stores its own copies, untouched by the trip.
    trip_sources.events = [RawEvent(title="Green Mill Jazz", start=at(3, 21), venue_name="Green Mill")]
    svc.run()
    home = svc.db.upcoming_events(at(-1), at(30))
    assert [e.trip_id for e in home] == [None]
    assert len(trip_events(svc.db, trip)) == 3

    svc.db.delete_trip(trip.id)  # deleting the trip deletes its events, not home ones
    assert trip_events(svc.db, trip) == [] and len(svc.db.upcoming_events(at(-1), at(30))) == 1


def test_trips_web_flow(cfg, trip_sources, monkeypatch):
    monkeypatch.setattr("eventscout.web.app.geocode_place", lambda q, c: CHICAGO)
    svc = Service(cfg)
    svc._llm = FakeLLM()
    client = TestClient(create_app(svc))
    assert client.get("/trips").status_code == 200

    start = datetime.now(CHI).date() + timedelta(days=3)
    bad = client.post("/api/trips", json={"destination": "Chicago", "start_date": str(start),
                                          "end_date": str(start - timedelta(days=1))})
    assert bad.status_code == 422 and "ends before" in bad.json()["detail"]

    r = client.post("/api/trips", json={"destination": "Chicago, IL", "start_date": str(start),
                                        "end_date": str(start + timedelta(days=2)), "radius_miles": 8,
                                        "notes": "No car", "discover": True})
    assert r.status_code == 200
    trip_id = r.json()["id"]
    for _ in range(100):  # built in the background
        if client.get(f"/api/trips/{trip_id}").json()["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert client.get(f"/api/trips/{trip_id}").json()["status"] == "done"

    page = client.get(f"/trips/{trip_id}").text
    assert "Green Mill Jazz" in page and "Second City Comedy" in page and "Timeshare Seminar" not in page  # picks
    assert "8 PM" in page and "Picks (2)" in page and "All (3)" in page
    assert "Timeshare Seminar" in client.get(f"/trips/{trip_id}?view=all").text
    assert 'id="map-data"' in client.get(f"/trips/{trip_id}?show=map").text
    ics = client.get(f"/trips/{trip_id}/calendar.ics")
    assert ics.status_code == 200 and b"Green Mill Jazz" in ics.content and b"Timeshare" not in ics.content
    assert "Green Mill Jazz" not in client.get("/?view=all").text  # home dashboard never shows trip events
    assert "Chicago, Illinois" in client.get("/trips").text

    assert client.delete(f"/api/trips/{trip_id}").status_code == 200
    assert client.get(f"/trips/{trip_id}").status_code == 404


def test_dest_key_is_shared_by_trips_to_the_same_city(cfg):
    svc = Service(cfg)
    a, b = make_trip(svc), make_trip(svc, start_in=40)
    assert a.dest_key == b.dest_key == "chicago|us-il|us"
    paris = a.model_copy(update={"city": "Paris", "region_code": "FR-IDF", "country_code": "fr"})
    texas = a.model_copy(update={"city": "Paris", "region_code": "US-TX", "country_code": "us"})
    assert paris.dest_key != texas.dest_key


def test_saved_city_sources_are_used_by_every_trip_there(cfg, trip_sources):
    svc = Service(cfg)
    svc._llm = FakeLLM()
    first = make_trip(svc)
    dest = svc.db.ensure_destination(first.dest_key, first.name)
    sid = svc.db.add_destination_source(dest.key, "Green Mill calendar", "https://greenmill.example/cal", "fake")
    off = svc.db.add_destination_source(dest.key, "Dead blog", "https://dead.example/", "fake")
    svc.db.update_destination_source(off, enabled=False)

    build_trip(svc, first.id)
    log = {s["name"]: s for s in svc.db.get_trip(first.id).sources}
    assert log["Green Mill calendar"]["origin"] == "saved" and log["Green Mill calendar"]["saved_id"] == sid
    assert "Dead blog" not in log  # switched off
    assert svc.db.get_destination_source(sid).last_event_count == 3

    # Turn the built-in off for Chicago: a later trip there uses only the saved source.
    svc.db.set_builtin_enabled(dest.key, "fakebrite", False)
    second = make_trip(svc, start_in=3)
    build_trip(svc, second.id)
    assert [s["name"] for s in svc.db.get_trip(second.id).sources] == ["Green Mill calendar"]
    assert len(trip_events(svc.db, svc.db.get_trip(second.id))) == 3


def test_trip_page_curates_city_sources(cfg, trip_sources, monkeypatch):
    from eventscout.detect import Detection

    monkeypatch.setattr("eventscout.web.app.detect_kind", lambda url, f, ctx: Detection("ical", url + "feed.ics", "linked iCal feed"))
    monkeypatch.setattr("eventscout.web.app.trial_fetch", lambda det, name, ctx: [object(), object()])
    svc = Service(cfg)
    svc._llm = FakeLLM()
    trip = make_trip(svc)
    build_trip(svc, trip.id)
    svc.db.update_trip(trip.id, sources=svc.db.get_trip(trip.id).sources + [
        {"name": "Chicago Reader calendar", "url": "https://reader.example/events", "kind": "web", "events": 9,
         "error": None, "origin": "discovered", "why": "weekly arts listings"}])
    client = TestClient(create_app(svc))

    page = client.get(f"/trips/{trip.id}").text
    assert "Sources for Chicago" in page and "Save for Chicago" in page and "Chicago Reader calendar" in page

    r = client.post(f"/api/trips/{trip.id}/sources", json={"url": "https://hideout.example/", "name": "The Hideout"})
    assert r.status_code == 200 and r.json() == {"id": r.json()["id"], "kind": "ical", "events_found": 2}
    hideout = r.json()["id"]
    assert client.post(f"/api/trips/{trip.id}/sources", json={"url": "https://hideout.example/"}).status_code == 409

    saved = client.post(f"/api/trips/{trip.id}/sources", json={"name": "Chicago Reader calendar", "kind": "web",
                                                              "url": "https://reader.example/events", "why": "weekly"})
    assert saved.status_code == 200
    page = client.get(f"/trips/{trip.id}").text
    assert "Save for Chicago" not in page  # already saved, so no longer offered
    names = [s.name for s in svc.db.list_destination_sources(trip.dest_key)]
    assert names == ["Chicago Reader calendar", "The Hideout"]

    assert client.post(f"/api/trips/{trip.id}/sources/{hideout}", json={"enabled": False}).status_code == 200
    assert not svc.db.get_destination_source(hideout).enabled
    assert client.post(f"/api/trips/{trip.id}/builtins/fakebrite", json={"enabled": False}).status_code == 200
    assert svc.db.get_destination(trip.dest_key).builtins_off == ["fakebrite"]
    assert client.post(f"/api/trips/{trip.id}/builtins/nope", json={"enabled": False}).status_code == 404
    assert client.delete(f"/api/trips/{trip.id}/sources/{hideout}").status_code == 200

    # A source saved for another city can't be changed through this trip.
    other = svc.db.ensure_destination("lisbon||pt", "Lisbon")
    foreign = svc.db.add_destination_source(other.key, "X", "https://x.example/", "web")
    assert client.delete(f"/api/trips/{trip.id}/sources/{foreign}").status_code == 404

    assert "Saved sources:" in client.get("/trips").text and "Chicago · 1" in client.get("/trips").text
