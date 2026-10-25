"""Web dashboard, calendar feeds and JSON API."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from eventscout.config import Profile
from eventscout.configfile import ConfigFileError, text_hash
from eventscout.dates import now
from eventscout.models import Trip
from eventscout.sources import Context
from eventscout.trips import (TripError, build_trip, builtin_sources, geocode_place, trip_config, trip_events,
                              trip_today, trip_window, validate_dates)
from eventscout.detect import detect_kind, trial_fetch
from eventscout.http import Fetcher, FetchError
from eventscout.llm import LLMError
from eventscout.pipeline import Service
from eventscout.report import env, feed_url, group_by_day, recommended
from eventscout.report.ics import build_calendar

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def map_places(events: list, today: dt.date) -> tuple[list[dict], int]:
    """Events grouped into one pin per location (a venue's shows share a pin). Events without exact coordinates
    (online, not geocoded, or only located to their city centre) are counted but not placed."""
    from eventscout.report import _fmt_time

    places: dict[tuple[float, float], dict] = {}
    unmapped = 0
    for e in events:
        if e.latitude is None or e.longitude is None or e.location_approx or e.online:
            unmapped += 1
            continue
        key = (round(e.latitude, 4), round(e.longitude, 4))  # ~10 m: same building
        place = places.setdefault(key, {"lat": key[0], "lon": key[1], "name": e.location_display,
                                        "miles": e.distance_miles, "best": None, "events": []})
        if e.score is not None:
            place["best"] = max(place["best"] or 0, e.score)
        day = e.start.date()
        place["events"].append({
            "id": e.id, "title": e.title, "url": e.url, "score": e.score, "cost": e.cost_display,
            "when": ("Today" if day == today else "Tomorrow" if day == today + timedelta(days=1)
                     else e.start.strftime("%a %b %-d")) + " · " + _fmt_time(e),
            "image": e.image_url, "starred": e.starred,
        })
    return list(places.values()), unmapped


def _date_links(today: dt.date, last_day: dt.date) -> list[tuple[str, dt.date]]:
    """Quick picks under the date box: Today, Tomorrow, and the next weekend's Saturday and Sunday."""
    links = [("Today", today), ("Tomorrow", today + timedelta(days=1))]
    for offset in range(2, 9):  # up to 8 days out, so a Saturday is always followed by its Sunday
        d = today + timedelta(days=offset)
        if d.weekday() in (5, 6):  # Saturday, Sunday
            links.append((d.strftime("%a %-d"), d))
        if d.weekday() == 6:
            break
    return [(label, d) for label, d in links if d <= last_day]


def _opt_num(value: str, kind: type[int] | type[float]) -> int | float | None:
    try:
        num = kind(float(value)) if kind is int else kind(value)
    except (TypeError, ValueError):
        return None
    return num if num == num and num >= 0 else None  # rejects NaN and negatives


class Feedback(BaseModel):
    value: int


class Star(BaseModel):
    starred: bool


class NewSource(BaseModel):
    url: str
    name: str | None = None


class SourceStatus(BaseModel):
    status: str


class NewTrip(BaseModel):
    destination: str
    start_date: dt.date
    end_date: dt.date
    radius_miles: float = 10
    near: str = ""
    notes: str = ""
    discover: bool = True


class TripSource(BaseModel):
    url: str
    name: str = ""
    kind: str | None = None  # given when saving a source the web search found (already checked)
    why: str = ""


class Enabled(BaseModel):
    enabled: bool


class RawConfig(BaseModel):
    text: str
    base_hash: str


def create_app(svc: Service) -> FastAPI:
    cfg = svc.cfg
    app = FastAPI(title="EventScout", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    basic = HTTPBasic(auto_error=False)

    def _basic_ok(creds: HTTPBasicCredentials | None) -> bool:
        if not (cfg.web.username and cfg.web.password):
            return True
        return bool(creds) and secrets.compare_digest(creds.username, cfg.web.username) and secrets.compare_digest(
            creds.password, cfg.web.password
        )

    def require_auth(creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        if not _basic_ok(creds):
            raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="EventScout"'})

    def require_feed_auth(request: Request, creds: HTTPBasicCredentials | None = Depends(basic)) -> None:
        # Calendar apps can't do basic auth well, so feeds also accept ?token=<report.feed_token>.
        token = cfg.report.feed_token
        if token and secrets.compare_digest(request.query_params.get("token", ""), token):
            return
        if _basic_ok(creds) and (not token or (cfg.web.username and creds)):
            return
        raise HTTPException(401, headers={"WWW-Authenticate": 'Basic realm="EventScout"'})

    def _bg(fn, *args, **kwargs) -> None:
        def target():
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                log.exception("background job failed")

        threading.Thread(target=target, daemon=True).start()

    def page(name: str, **ctx) -> HTMLResponse:
        last = svc.db.recent_runs(1)
        return HTMLResponse(env.get_template(name).render(
            cfg=cfg, running=svc.is_running, last_run=last[0] if last else None, **ctx
        ))

    # ------------------------------------------------------------------ pages
    def listing(view: str, q: str, days: str, date: str, min_score: str, max_miles: str, free: str,
                tag: str) -> tuple[list, dict]:
        """Events for a view + filters, and the template context shared by the list and the map."""
        # The filter form submits empty boxes as "", so numeric filters arrive as text: blank or junk means "not set".
        days = _opt_num(days, int)
        min_score = _opt_num(min_score, int)
        max_miles = _opt_num(max_miles, float)
        free = free.lower() in ("1", "true", "on", "yes")
        horizon = days or cfg.search.horizon_days
        today = now(svc.tz).date()
        last_day = today + timedelta(days=cfg.search.horizon_days)
        try:  # a specific day overrides "days ahead"; past dates snap to today
            day = max(dt.date.fromisoformat(date), today) if date else None
        except ValueError:
            day = None
        if day:
            # Today starts now (so finished events drop off); a future day starts at midnight.
            start = now(svc.tz) if day == today else dt.datetime.combine(day, dt.time(0), tzinfo=svc.tz)
            end = dt.datetime.combine(day, dt.time(23, 59), tzinfo=svc.tz)
        else:
            start, end = now(svc.tz), now(svc.tz) + timedelta(days=horizon)
        if view == "picks":
            events = recommended(svc, min_score=min_score, start=start, end=end)
        else:
            where = {
                "all": "status='ok'",
                "starred": "starred",
                "hidden": "feedback < 0",
                "filtered": "status='filtered'",
                "unscored": "status='ok' AND score IS NULL",
            }.get(view, "status='ok'")
            events = svc.db.upcoming_events(start, end, where)
            if min_score is not None:
                events = [e for e in events if (e.score or 0) >= min_score]
        if q:
            ql = q.lower()
            events = [e for e in events if ql in f"{e.title} {e.description} {e.venue_name} {e.city}".lower()]
        if max_miles is not None:
            events = [e for e in events if e.distance_miles is not None and e.distance_miles <= max_miles]
        if free:
            events = [e for e in events if e.is_free or e.price_min == 0]
        if tag:
            events = [e for e in events if tag in e.tags or tag in e.categories]
        filters = {"q": q, "days": days or "", "date": day.isoformat() if day else "",
                   "min_score": min_score if min_score is not None else "",
                   "max_miles": max_miles if max_miles is not None else "", "free": free, "tag": tag}
        return events, dict(
            count=len(events), view=view, day=day, beyond_horizon=bool(day and day > last_day),
            date_links=_date_links(today, last_day), today=today, last_day=last_day, filters=filters,
            all_tags=sorted({t for e in events for t in e.tags}),
            ics_url=feed_url(svc, "/calendar.ics"), starred_ics_url=feed_url(svc, "/calendar/starred.ics"),
        )

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def dashboard(view: str = "picks", q: str = "", days: str = "", date: str = "", min_score: str = "",
                  max_miles: str = "", free: str = "", tag: str = ""):
        events, ctx = listing(view, q, days, date, min_score, max_miles, free, tag)
        return page("dashboard.html", groups=group_by_day(events, ctx["today"]), base_path="/", **ctx)

    @app.get("/map", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def map_page(view: str = "picks", q: str = "", days: str = "", date: str = "", min_score: str = "",
                 max_miles: str = "", free: str = "", tag: str = ""):
        events, ctx = listing(view, q, days, date, min_score, max_miles, free, tag)
        places, unmapped = map_places(events, ctx["today"])
        return page("map.html", base_path="/map", places=places, unmapped=unmapped,
                    home={"lat": cfg.home.latitude, "lon": cfg.home.longitude, "label": cfg.home.label,
                          "radius_miles": cfg.search.max_distance_miles},
                    tiles=cfg.web.map_tiles(), **ctx)

    @app.get("/sources", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def sources_page():
        return page("sources.html", sources=svc.db.list_sources())

    @app.get("/runs", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def runs_page():
        return page("runs.html", runs=svc.db.recent_runs(40))

    # ------------------------------------------------------------------ feeds
    @app.get("/calendar.ics", dependencies=[Depends(require_feed_auth)])
    def calendar_all():
        return Response(build_calendar(recommended(svc), "EventScout picks", cfg.report.public_base_url),
                        media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="eventscout.ics"'})

    @app.get("/calendar/starred.ics", dependencies=[Depends(require_feed_auth)])
    def calendar_starred():
        events = svc.db.upcoming_events(now(svc.tz), now(svc.tz) + timedelta(days=365), "starred")
        return Response(build_calendar(events, "EventScout — going", cfg.report.public_base_url),
                        media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="eventscout-going.ics"'})

    @app.get("/events/{event_id}.ics", dependencies=[Depends(require_feed_auth)])
    def event_ics(event_id: int):
        ev = svc.db.get_event(event_id)
        if not ev:
            raise HTTPException(404)
        return Response(build_calendar([ev], ev.title, cfg.report.public_base_url), media_type="text/calendar",
                        headers={"Content-Disposition": f'attachment; filename="event-{event_id}.ics"'})

    @app.get("/report.html", dependencies=[Depends(require_auth)])
    def static_report():
        path = Path(cfg.report.output_dir) / "report.html"
        if not path.exists():
            raise HTTPException(404, "no report yet — run once first")
        return FileResponse(path)

    @app.get("/events.json", dependencies=[Depends(require_feed_auth)])
    def events_json():
        return JSONResponse([e.model_dump(mode="json") for e in recommended(svc)])

    # -------------------------------------------------------------------- API
    @app.post("/api/events/{event_id}/feedback", dependencies=[Depends(require_auth)])
    def feedback(event_id: int, body: Feedback):
        if body.value not in (-1, 0, 1):
            raise HTTPException(400, "value must be -1, 0 or 1")
        svc.db.set_feedback(event_id, body.value)
        return {"ok": True}

    @app.post("/api/events/{event_id}/star", dependencies=[Depends(require_auth)])
    def star(event_id: int, body: Star):
        svc.db.set_starred(event_id, body.starred)
        return {"ok": True}

    @app.post("/api/run", dependencies=[Depends(require_auth)])
    def run_now(discover: bool = False):
        if svc.is_running:
            raise HTTPException(409, "a run is already in progress")
        _bg(svc.run, discover=discover)
        return {"started": True}

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    def status():
        last = svc.db.recent_runs(1)
        return {"running": svc.is_running, "last_run": last[0] if last else None}

    @app.post("/api/sources", dependencies=[Depends(require_auth)])
    def add_source(body: NewSource):
        fetcher = Fetcher(cfg.fetch)
        try:
            ctx = svc.context(fetcher)
            det = detect_kind(body.url.strip(), fetcher, ctx)
            events = trial_fetch(det, body.name or body.url, ctx)
        except (FetchError, LLMError, ValueError) as exc:
            raise HTTPException(422, f"Could not read that source: {exc}")
        finally:
            fetcher.close()
        name = body.name or det.url.split("//", 1)[-1].split("/", 1)[0]
        sid = svc.db.add_source(name, det.url, det.kind, status="active", origin="manual", notes=det.note)
        if not sid:
            raise HTTPException(409, "that source already exists")
        warning = svc.add_source_to_config(sid, name, det.url, det.kind)
        return {"id": sid, "kind": det.kind, "events_found": len(events), "note": det.note,
                "saved_to_config": warning is None, "warning": warning}

    @app.post("/api/sources/{source_id}/status", dependencies=[Depends(require_auth)])
    def source_status(source_id: int, body: SourceStatus):
        if body.status not in ("active", "trial", "disabled", "retired"):
            raise HTTPException(400)
        src = svc.db.get_source(source_id)
        if src is None:
            raise HTTPException(404)
        warning = None
        if src.origin == "seed" and body.status in ("active", "disabled"):
            # Seeds are defined in config.yaml; record the switch there too, or a restart would undo it.
            try:
                svc.config_file.set_source_enabled(src.url, body.status == "active")
            except ConfigFileError as exc:
                warning = f"changed for now, but not saved to config.yaml (a restart will undo it): {exc}"
        fields = {"status": body.status}
        if body.status == "active":
            fields["consecutive_failures"] = 0
        svc.db.update_source(source_id, **fields)
        return {"ok": True, "warning": warning}

    @app.delete("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
    def delete_source(source_id: int):
        src = svc.db.get_source(source_id)
        if src is None:
            raise HTTPException(404)
        if src.origin == "seed" and svc.config_file.has_source(src.url):
            try:
                svc.config_file.remove_source(src.url)
            except ConfigFileError as exc:
                raise HTTPException(409, f"can't remove it from config.yaml, so it would come back on restart: {exc}")
        svc.db.delete_source(source_id)
        svc.reload_config()
        return {"ok": True}

    # ------------------------------------------------------------------ trips
    def start_trip_build(trip_id: int) -> None:
        def build():
            with svc.trip_lock:  # queued trips wait their turn
                build_trip(svc, trip_id)

        _bg(build)

    @app.get("/trips", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def trips_page():
        today = now(svc.tz).date()
        return page("trips.html", trips=svc.db.list_trips(), today=today,
                    destinations=[d for d in svc.db.list_destinations() if d.source_count or d.builtins_off],
                    default_start=today + timedelta(days=7), default_end=today + timedelta(days=9),
                    can_discover=cfg.discovery.enabled)

    @app.post("/api/trips", dependencies=[Depends(require_auth)])
    def create_trip(body: NewTrip):
        try:
            place = geocode_place(body.destination.strip(), cfg)
            validate_dates(body.start_date, body.end_date, now(ZoneInfo(place.timezone)).date())
            lat, lon = place.latitude, place.longitude
            if body.near.strip():  # "staying near" moves the centre the radius is measured from
                spot = geocode_place(f"{body.near.strip()}, {place.label}", cfg)
                lat, lon = spot.latitude, spot.longitude
        except TripError as exc:
            raise HTTPException(422, str(exc))
        trip_id = svc.db.create_trip(
            name=place.label, query=body.destination.strip(), latitude=lat, longitude=lon, timezone=place.timezone,
            city=place.name, region=place.region, region_code=place.region_code, country=place.country,
            country_code=place.country_code, start_date=body.start_date, end_date=body.end_date,
            radius_miles=max(1.0, min(body.radius_miles, 100.0)), near=body.near.strip() or None,
            notes=body.notes.strip(), discover=body.discover and cfg.discovery.enabled,
        )
        start_trip_build(trip_id)
        return {"id": trip_id, "name": place.label, "timezone": place.timezone}

    def _trip_or_404(trip_id: int) -> Trip:
        trip = svc.db.get_trip(trip_id)
        if trip is None:
            raise HTTPException(404, "no such trip")
        return trip

    @app.get("/trips/{trip_id}", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def trip_page(trip_id: int, view: str = "picks", show: str = "list"):
        trip = _trip_or_404(trip_id)
        everything = trip_events(svc.db, trip)
        picks = [e for e in everything if (e.score or 0) >= cfg.profile.min_score or e.starred]
        events = everything if view == "all" else picks
        today = trip_today(trip)
        places, unmapped = map_places(events, today)
        dest = svc.db.ensure_destination(trip.dest_key, trip.name)
        saved = svc.db.list_destination_sources(dest.key)
        saved_urls = {s.url.rstrip("/") for s in saved}
        last_run = {s.get("builtin"): s for s in trip.sources if s.get("origin") == "built-in"}
        builtins = [{**b, "enabled": b["builtin"] not in dest.builtins_off, "last": last_run.get(b["builtin"])}
                    for b in builtin_sources(trip, cfg)]
        found = [s for s in trip.sources if s.get("origin") == "discovered" and s["url"].rstrip("/") not in saved_urls]
        return page(
            "trip.html", trip=trip, view=view, show=show, events=events, groups=group_by_day(events, today),
            dest=dest, saved_sources=saved, builtins=builtins, found_sources=found,
            pick_count=len(picks), all_count=len(everything), places=places, unmapped=unmapped,
            home={"lat": trip.latitude, "lon": trip.longitude, "label": trip.near or trip.name,
                  "radius_miles": trip.radius_miles},
            tiles=cfg.web.map_tiles(),
            feedback_count=len(svc.db.feedback_examples(200)),
        )

    @app.get("/api/trips/{trip_id}", dependencies=[Depends(require_auth)])
    def trip_status(trip_id: int):
        trip = _trip_or_404(trip_id)
        return {"status": trip.status, "log": trip.log.splitlines()[-12:], "stats": trip.stats}

    @app.post("/api/trips/{trip_id}/rebuild", dependencies=[Depends(require_auth)])
    def rebuild_trip(trip_id: int):
        trip = _trip_or_404(trip_id)
        if trip.status in ("queued", "running"):
            raise HTTPException(409, "this trip is already being built")
        svc.db.update_trip(trip_id, status="queued")
        start_trip_build(trip_id)
        return {"ok": True}

    # Saved sources belong to the trip's city, so every trip there (and every rebuild) uses them.
    def _trip_source_or_404(trip: Trip, source_id: int):
        src = svc.db.get_destination_source(source_id)
        if src is None or src.dest_key != trip.dest_key:
            raise HTTPException(404, "no such source for this city")
        return src

    @app.post("/api/trips/{trip_id}/sources", dependencies=[Depends(require_auth)])
    def add_trip_source(trip_id: int, body: TripSource):
        trip = _trip_or_404(trip_id)
        dest = svc.db.ensure_destination(trip.dest_key, trip.name)
        url = body.url.strip()
        if body.kind:  # found by the web search during a build; it was already checked then
            kind, note, found = body.kind, body.why or "found by web search", None
        else:
            fetcher = Fetcher(cfg.fetch)
            try:
                tcfg = trip_config(cfg, trip)
                start, end = trip_window(trip)
                ctx = Context(cfg=tcfg, fetcher=fetcher, llm_factory=lambda: svc.llm, window_start=start,
                              window_end=end, db=svc.db)
                det = detect_kind(url, fetcher, ctx)
                found = len(trial_fetch(det, body.name or url, ctx))
                url, kind, note = det.url, det.kind, det.note
            except (FetchError, LLMError, ValueError) as exc:
                raise HTTPException(422, f"Could not read that source: {exc}")
            finally:
                fetcher.close()
        name = body.name.strip() or url.split("//", 1)[-1].split("/", 1)[0]
        sid = svc.db.add_destination_source(dest.key, name, url, kind, origin="discovered" if body.kind else "manual",
                                            notes=note)
        if sid is None:
            raise HTTPException(409, f"{trip.city} already has that source")
        return {"id": sid, "kind": kind, "events_found": found}

    @app.post("/api/trips/{trip_id}/sources/{source_id}", dependencies=[Depends(require_auth)])
    def toggle_trip_source(trip_id: int, source_id: int, body: Enabled):
        _trip_source_or_404(_trip_or_404(trip_id), source_id)
        svc.db.update_destination_source(source_id, enabled=body.enabled)
        return {"ok": True}

    @app.delete("/api/trips/{trip_id}/sources/{source_id}", dependencies=[Depends(require_auth)])
    def remove_trip_source(trip_id: int, source_id: int):
        _trip_source_or_404(_trip_or_404(trip_id), source_id)
        svc.db.delete_destination_source(source_id)
        return {"ok": True}

    @app.post("/api/trips/{trip_id}/builtins/{builtin}", dependencies=[Depends(require_auth)])
    def toggle_trip_builtin(trip_id: int, builtin: str, body: Enabled):
        trip = _trip_or_404(trip_id)
        if builtin not in {b["builtin"] for b in builtin_sources(trip, cfg)}:
            raise HTTPException(404, "no such built-in source")
        svc.db.ensure_destination(trip.dest_key, trip.name)
        svc.db.set_builtin_enabled(trip.dest_key, builtin, body.enabled)
        return {"ok": True}

    @app.delete("/api/trips/{trip_id}", dependencies=[Depends(require_auth)])
    def delete_trip(trip_id: int):
        trip = _trip_or_404(trip_id)
        if trip.status == "running":
            raise HTTPException(409, "wait for the build to finish before deleting this trip")
        svc.db.delete_trip(trip_id)
        return {"ok": True}

    @app.get("/trips/{trip_id}/calendar.ics", dependencies=[Depends(require_feed_auth)])
    def trip_ics(trip_id: int, view: str = "picks"):
        trip = _trip_or_404(trip_id)
        events = trip_events(svc.db, trip)
        if view != "all":
            events = [e for e in events if (e.score or 0) >= cfg.profile.min_score or e.starred]
        name = f"EventScout — {trip.city or trip.name} {trip.start_date:%b %-d}"
        return Response(build_calendar(events, name, cfg.report.public_base_url), media_type="text/calendar",
                        headers={"Content-Disposition": f'attachment; filename="trip-{trip_id}.ics"'})

    # --------------------------------------------------------------- settings
    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def settings_page():
        text = svc.config_file.read_text()
        return page("settings.html", profile=cfg.profile, config_text=text, config_hash=text_hash(text),
                    config_path=str(svc.config_file.path), read_only=svc.config_file.read_only_reason())

    @app.post("/api/profile", dependencies=[Depends(require_auth)])
    def save_profile(body: dict):
        try:
            profile = Profile.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5]))
        before = cfg.profile.fingerprint()
        try:
            svc.config_file.update_profile(profile)
        except ConfigFileError as exc:
            raise HTTPException(409, str(exc))
        svc.reload_config()
        return {"ok": True, "rescore": cfg.profile.fingerprint() != before}

    @app.post("/api/config", dependencies=[Depends(require_auth)])
    def save_config(body: RawConfig):
        try:
            svc.config_file.write_text(body.text, base_hash=body.base_hash)
        except ConfigFileError as exc:
            raise HTTPException(409 if "changed on disk" in str(exc) else 422, str(exc))
        restart = svc.reload_config()
        return {"ok": True, "restart_needed": restart, "hash": text_hash(svc.config_file.read_text())}

    @app.get("/healthz")
    def healthz():
        if not svc.db.ping():
            return JSONResponse({"ok": False, "database": "unreachable"}, status_code=503)
        return {"ok": True}

    return app
