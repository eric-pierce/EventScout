"""Web dashboard, calendar feeds and JSON API."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from eventscout.dates import now
from eventscout.detect import detect_kind, trial_fetch
from eventscout.http import Fetcher, FetchError
from eventscout.llm import LLMError
from eventscout.pipeline import Service
from eventscout.report import env, feed_url, group_by_day, recommended
from eventscout.report.ics import build_calendar

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


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
    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
    def dashboard(view: str = "picks", q: str = "", days: str = "", date: str = "", min_score: str = "",
                  max_miles: str = "", free: str = "", tag: str = ""):
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
                "starred": "starred=1",
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
        all_tags = sorted({t for e in events for t in e.tags})
        return page(
            "dashboard.html", groups=group_by_day(events, today), count=len(events), view=view,
            day=day, beyond_horizon=bool(day and day > last_day), date_links=_date_links(today, last_day),
            today=today, last_day=last_day,
            filters={"q": q, "days": days or "", "date": day.isoformat() if day else "",
                     "min_score": min_score if min_score is not None else "",
                     "max_miles": max_miles if max_miles is not None else "", "free": free, "tag": tag},
            all_tags=all_tags,
            ics_url=feed_url(svc, "/calendar.ics"), starred_ics_url=feed_url(svc, "/calendar/starred.ics"),
        )

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
        events = svc.db.upcoming_events(now(svc.tz), now(svc.tz) + timedelta(days=365), "starred = 1")
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
        return {"id": sid, "kind": det.kind, "events_found": len(events), "note": det.note}

    @app.post("/api/sources/{source_id}/status", dependencies=[Depends(require_auth)])
    def source_status(source_id: int, body: SourceStatus):
        if body.status not in ("active", "trial", "disabled", "retired"):
            raise HTTPException(400)
        fields = {"status": body.status}
        if body.status == "active":
            fields["consecutive_failures"] = 0
        svc.db.update_source(source_id, **fields)
        return {"ok": True}

    @app.delete("/api/sources/{source_id}", dependencies=[Depends(require_auth)])
    def delete_source(source_id: int):
        svc.db.delete_source(source_id)
        return {"ok": True}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
