"""Trips: a one-time event report for a city you're visiting, ranked with the profile and feedback you've built at home.

A trip reuses the regular pipeline steps with a trip-specific copy of the config: the destination stands in for home,
the trip dates are the search window and its radius the distance limit. The trip's own notes are added to your
preferences. Its events are stored with the trip's id, so they never appear in the home dashboard, picks, email or
calendar feeds, and they're deleted with the trip.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from tzfpy import get_tz

from eventscout.config import Config, Home
from eventscout.dates import now
from eventscout.db import Database, domain_of
from eventscout.http import Fetcher, FetchError
from eventscout.llm import LLM, LLMError
from eventscout.models import Source, Trip, dest_key_for
from eventscout.pipeline import PipelineSteps, RunLog
from eventscout.sources import Context

log = logging.getLogger("eventscout.trips")

MAX_TRIP_DAYS = 31


class TripError(Exception):
    pass


# --------------------------------------------------------------------------- places
@dataclass
class Place:
    name: str  # "Chicago"
    label: str  # "Chicago, Illinois, United States"
    latitude: float
    longitude: float
    timezone: str
    region: str | None
    region_code: str | None  # "US-IL"
    country: str | None
    country_code: str | None  # "us"

    @property
    def dest_key(self) -> str:
        return dest_key_for(self.name, self.region_code, self.country_code)

    def as_dict(self) -> dict:
        return asdict(self)


def place_of(trip: Trip) -> dict:
    """The Place fields of a trip's city (stored on the city so it can be curated without a trip)."""
    return Place(name=trip.city or trip.name, label=trip.name, latitude=trip.latitude, longitude=trip.longitude,
                 timezone=trip.timezone, region=trip.region, region_code=trip.region_code, country=trip.country,
                 country_code=trip.country_code).as_dict()


CITY_CHECK_DAYS = 14  # with no trip yet, new sources are checked against the next two weeks


def city_stand_in(place: dict) -> Trip:
    """A stand-in trip for curating a city's sources before any trip there: the next two weeks, from the centre."""
    p = Place(**{k: place.get(k) for k in Place.__dataclass_fields__})
    today = now(ZoneInfo(p.timezone)).date()
    return Trip(id=0, name=p.label, query=p.label, latitude=p.latitude, longitude=p.longitude, timezone=p.timezone,
                city=p.name, region=p.region, region_code=p.region_code, country=p.country, country_code=p.country_code,
                start_date=today, end_date=today + timedelta(days=CITY_CHECK_DAYS), discover=False, status="done",
                created_at=datetime.now(timezone.utc))


def geocode_place(query: str, cfg: Config, *, client: httpx.Client | None = None) -> Place:
    """Resolve a destination ("Chicago, IL", "Lisbon") with OpenStreetMap Nominatim; time zone from coordinates."""
    if not cfg.geocoding.nominatim_url:
        raise TripError("geocoding is disabled (geocoding.nominatim_url), so destinations can't be looked up")
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": cfg.fetch.user_agent}, timeout=20)
    try:
        resp = client.get(cfg.geocoding.nominatim_url.rstrip("/") + "/search",
                          params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1})
        resp.raise_for_status()
        hits = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TripError(f"couldn't look up {query!r}: {exc}") from exc
    finally:
        if own:
            client.close()
    if not hits:
        raise TripError(f"couldn't find a place called {query!r}; try adding the state or country")
    hit, addr = hits[0], hits[0].get("address", {})
    lat, lon = float(hit["lat"]), float(hit["lon"])
    name = hit.get("name") or addr.get("city") or addr.get("town") or query.split(",")[0].strip()
    parts = [name, addr.get("state"), addr.get("country")]
    return Place(
        name=name, label=", ".join(dict.fromkeys(p for p in parts if p)), latitude=lat, longitude=lon,
        timezone=get_tz(lon, lat) or cfg.home.timezone, region=addr.get("state"),
        region_code=addr.get("ISO3166-2-lvl4"), country=addr.get("country"), country_code=addr.get("country_code"),
    )


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ------------------------------------------------------------------------- sources
def builtin_sources(trip: Trip, cfg: Config) -> list[dict]:
    """Aggregators that work for most cities, pointed at the trip's city and (where supported) its dates."""
    city = _slug(trip.city or trip.name.split(",")[0])
    state = (trip.region_code or "").split("-")[-1].lower() if trip.country_code == "us" else None
    out: list[dict] = []
    area = f"{state}--{city}" if state else f"{_slug(trip.country or '')}--{city}" if trip.country else None
    if area:
        out.append({
            "builtin": "eventbrite", "name": f"Eventbrite — {trip.city}", "kind": "web", "options": {"pages": 4},
            "url": f"https://www.eventbrite.com/d/{area}/all-events/?start_date={trip.start_date}&end_date={trip.end_date}",
        })
    out.append({"builtin": "allevents", "name": f"AllEvents — {trip.city}", "kind": "web", "options": {},
                "url": f"https://allevents.in/{city}"})
    if state:
        out.append({"builtin": "meetup", "name": f"Meetup — {trip.city}", "kind": "web", "options": {},
                    "url": f"https://www.meetup.com/find/?location=us--{state}--{quote(trip.city or '')}&source=EVENTS"})
    # Ticketmaster covers the whole area by coordinates and dates, if you've given it a key.
    tm = next((s for s in cfg.sources if s.kind == "ticketmaster" and (s.options or {}).get("api_key")), None)
    if tm:
        out.append({"builtin": "ticketmaster", "name": f"Ticketmaster — {trip.city}", "kind": "ticketmaster", "url": tm.url,
                    "options": {**tm.options, "max_pages": 3}})
    return out


# -------------------------------------------------------------------------- config
def trip_config(cfg: Config, trip: Trip) -> Config:
    """The home config with the trip standing in for home: location, time zone, dates, radius and trip notes."""
    t = cfg.model_copy(deep=True)
    t.home = Home(label=trip.name, latitude=trip.latitude, longitude=trip.longitude, timezone=trip.timezone,
                  city=trip.city or trip.name, region=trip.region or "", country=(trip.country_code or "").upper(),
                  nearby=[])
    t.search.horizon_days = max(1, trip.nights)
    t.search.max_distance_miles = trip.radius_miles
    t.search.max_drive_minutes = None  # visitors often don't drive; the radius is the limit
    visiting = (f"They are visiting {trip.name} from {trip.start_date:%b %-d} to {trip.end_date:%b %-d, %Y} "
                f"(a trip away from home). Favor things that are distinctive to the city or hard to find at home; "
                f"their usual weekday routine doesn't apply unless the trip notes say so.")
    extra = f"\nTrip notes (these override their usual preferences): {trip.notes.strip()}" if trip.notes.strip() else ""
    t.profile.notes = (cfg.profile.notes.strip() + "\n\n" + visiting + extra).strip()
    t.sources = []
    t.discovery.max_candidates_per_run = min(cfg.discovery.max_candidates_per_run, 8)
    return t


def trip_window(trip: Trip) -> tuple[datetime, datetime]:
    tz = ZoneInfo(trip.timezone)
    start = datetime.combine(trip.start_date, dtime(0), tzinfo=tz)
    end = datetime.combine(trip.end_date, dtime(23, 59), tzinfo=tz)
    return max(start, now(tz)), end


def validate_dates(start: date, end: date, today: date) -> None:
    if end < start:
        raise TripError("the trip ends before it starts")
    if end < today:
        raise TripError("that trip is already over")
    if (end - start).days + 1 > MAX_TRIP_DAYS:
        raise TripError(f"trips can be at most {MAX_TRIP_DAYS} days")


# --------------------------------------------------------------------------- build
class TripBuild(PipelineSteps):
    """The pipeline steps, scoped to one trip's events and running on the trip's config."""

    def __init__(self, cfg: Config, db: Database, trip: Trip, llm_provider):
        self.cfg = cfg
        self.db = db
        self.trip = trip
        self.trip_id = trip.id
        self.tz = ZoneInfo(trip.timezone)
        self._llm_provider = llm_provider

    @property
    def llm(self) -> LLM:
        return self._llm_provider()


class _TripLog(RunLog):
    """A run log that's also saved on the trip every couple of seconds, so the page can show progress."""

    def __init__(self, db: Database, trip_id: int):
        super().__init__()
        self._db, self._trip_id, self._saved = db, trip_id, 0.0

    def __call__(self, msg: str, level: int = logging.INFO) -> None:
        super().__call__(msg, level)
        if time.monotonic() - self._saved > 2:
            self._saved = time.monotonic()
            self._db.update_trip(self._trip_id, log=self.text())


def _source(i: int, spec: dict) -> Source:
    # Trip sources aren't stored as source rows; a negative id keeps them apart from home sources.
    return Source(id=-(i + 1), name=spec["name"], url=spec["url"], kind=spec["kind"], status="active",
                  origin="trip", options=spec.get("options") or {}, added_at=datetime.now(timezone.utc))


def build_trip(svc, trip_id: int) -> dict:
    """Build (or rebuild) a trip's report. Runs the same steps as a regular run, once."""
    db: Database = svc.db
    trip = db.get_trip(trip_id)
    if trip is None:
        raise TripError(f"no trip {trip_id}")
    db.update_trip(trip_id, status="running", log="")
    rl = _TripLog(db, trip_id)
    tcfg = trip_config(svc.cfg, trip)
    start, end = trip_window(trip)
    fetcher = Fetcher(tcfg.fetch)
    ctx = Context(cfg=tcfg, fetcher=fetcher, llm_factory=lambda: svc.llm, window_start=start, window_end=end, db=db)
    steps = TripBuild(tcfg, db, trip, lambda: svc.llm)
    usage_before = svc._llm.usage.as_dict() if svc._llm is not None else {}
    t0 = time.monotonic()
    stats: dict = {}
    source_log: list[dict] = []
    status = "done"
    try:
        rl(f"building {trip.name}, {trip.dates_display}, within {trip.radius_miles:g} mi")
        dest = db.ensure_destination(trip.dest_key, trip.name, place_of(trip))
        specs = [s for s in builtin_sources(trip, svc.cfg) if s.get("builtin") not in dest.builtins_off]
        saved = db.list_destination_sources(dest.key, enabled_only=True)
        if saved:
            rl(f"using {len(saved)} saved source{'s' if len(saved) != 1 else ''} for {trip.city}")
        # Synthetic (negative) ids keep trip sources apart from home source rows; saved ones map back by id.
        sources = [_source(i, s) for i, s in enumerate(specs)]
        sources += [Source(id=-(1000 + s.id), name=s.name, url=s.url, kind=s.kind, status="active", origin="saved",
                           options=s.options, added_at=s.added_at) for s in saved]
        builtin_of = {-(i + 1): s.get("builtin") for i, s in enumerate(specs)}

        def record(src: Source, count: int, err: str | None) -> None:
            entry = {"name": src.name, "url": src.url, "kind": src.kind, "events": count, "error": err}
            if src.id <= -1000:  # a saved city source: remember how it did
                entry.update(origin="saved", saved_id=-src.id - 1000)
                db.update_destination_source(-src.id - 1000, last_event_count=count, last_error=err,
                                             last_built_at=datetime.now(timezone.utc))
            else:
                entry.update(origin="built-in", builtin=builtin_of.get(src.id))
            source_log.append(entry)

        stats["fetch"] = steps._fetch_all(ctx, rl, sources=sources, record=record)

        if trip.discover and svc.cfg.discovery.enabled:
            from eventscout.discovery import find_sources

            focus = (f"They're visiting from {trip.start_date:%b %-d} to {trip.end_date:%b %-d, %Y}: find pages "
                     f"listing events on those dates.")
            try:
                found, stats["discovery"] = find_sources(svc.llm, ctx, {domain_of(s.url) for s in sources}, rl, focus=focus)
                for i, f in enumerate(found, start=100_000):
                    src = _source(i, {"name": f.name, "url": f.det.url, "kind": f.det.kind})
                    for ev in f.events:  # already fetched while validating; no second fetch
                        db.upsert_event(ev, src, trip_id=trip.id)
                    source_log.append({"name": f.name, "url": f.det.url, "kind": f.det.kind, "events": len(f.events),
                                       "error": None, "origin": "discovered", "why": f.why})
            except (LLMError, FetchError) as exc:
                rl(f"source discovery failed, continuing with the built-in sources: {exc}", logging.WARNING)

        stats["locate"] = steps._locate(ctx, rl)
        stats["filter"] = steps._filter(ctx, rl)
        stats["score"] = steps._score(ctx, rl)
        stats["enrich"] = steps._enrich(ctx, rl)
        stats["images"] = steps._images(ctx, rl)
        picks = db.upcoming_events(start, end, "status='ok' AND score >= %s", (tcfg.profile.min_score,), trip_id=trip.id)
        stats["picks"] = len(picks)
        rl(f"done: {len(picks)} picks")
    except Exception as exc:  # noqa: BLE001 - record the failure on the trip
        status = "error"
        rl(f"build failed: {type(exc).__name__}: {exc}", logging.ERROR)
        log.exception("trip %s failed", trip_id)
    finally:
        fetcher.close()
        if svc._llm is not None:
            usage = svc._llm.usage.as_dict()
            stats["llm"] = {"provider": svc._llm.name, **{k: usage[k] - usage_before.get(k, 0) for k in usage}}
        stats["seconds"] = round(time.monotonic() - t0, 1)
        db.update_trip(trip_id, status=status, stats=stats, sources=source_log, log=rl.text(),
                       built_at=datetime.now(timezone.utc))
    return stats


def trip_events(db: Database, trip: Trip, where: str = "status='ok'", params: tuple = ()) -> list:
    """A trip's events with times shown in the destination's time zone."""
    tz = ZoneInfo(trip.timezone)
    start = datetime.combine(trip.start_date, dtime(0), tzinfo=tz)
    end = datetime.combine(trip.end_date, dtime(23, 59), tzinfo=tz)
    events = db.upcoming_events(start, end, where, params, trip_id=trip.id)
    for e in events:
        e.start = e.start.astimezone(tz)
        if e.end:
            e.end = e.end.astimezone(tz)
    return events


def trip_today(trip: Trip) -> date:
    return now(ZoneInfo(trip.timezone)).date()


__all__ = ["Place", "TripBuild", "TripError", "build_trip", "builtin_sources", "geocode_place", "trip_config",
           "trip_events", "trip_today", "trip_window", "validate_dates"]
