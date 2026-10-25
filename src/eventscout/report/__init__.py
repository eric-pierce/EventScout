"""Report building: selects recommended events and writes HTML/ICS/JSON outputs, optionally emailing a digest."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from jinja2 import Environment, PackageLoader, select_autoescape

from eventscout.dates import now
from eventscout.models import Event
from eventscout.report.ics import build_calendar
from eventscout.report import mailer

if TYPE_CHECKING:
    from collections.abc import Callable

    from eventscout.pipeline import RunLog, Service

log = logging.getLogger(__name__)

env = Environment(loader=PackageLoader("eventscout", "web/templates"), autoescape=select_autoescape(["html"]))


def _fmt_time(ev: Event) -> str:
    if ev.all_day:
        return "All day"
    if not ev.time_known:
        return "Time TBA"
    s = ev.start.strftime("%-I:%M %p").replace(":00 ", " ")
    if ev.end and ev.end.date() == ev.start.date():
        s += "–" + ev.end.strftime("%-I:%M %p").replace(":00 ", " ")
    elif ev.end:
        s += " – " + ev.end.strftime("%b %-d")
    return s


def _fmt_clock(hhmm: str) -> str:
    """ "17:00" -> "5 PM", "09:30" -> "9:30 AM" (for the time filter)."""
    try:
        return datetime.strptime(hhmm, "%H:%M").strftime("%-I:%M %p").replace(":00 ", " ")
    except ValueError:
        return hhmm


def _fmt_day(d: date, today: date) -> str:
    if d == today:
        return "Today · " + d.strftime("%A, %B %-d")
    if d == today + timedelta(days=1):
        return "Tomorrow · " + d.strftime("%A, %B %-d")
    return d.strftime("%A, %B %-d")


def _fmt_ts(value: object) -> str:
    """Format a stored UTC timestamp (str or datetime) in the home timezone."""
    if not value:
        return "—"
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    tz = env.globals.get("home_tz")
    return (dt.astimezone(tz) if tz else dt).strftime("%b %-d, %-I:%M %p")


env.filters["event_time"] = _fmt_time
env.filters["ts"] = _fmt_ts
env.filters["clock"] = _fmt_clock
_CSS_PATH = Path(__file__).resolve().parent.parent / "web" / "static" / "style.css"
env.globals["inline_css"] = lambda: _CSS_PATH.read_text()
# Cache-busting token for /static assets, so browsers pick up new CSS/JS after an upgrade.
env.globals["asset_v"] = lambda: f"{int(max(p.stat().st_mtime for p in _CSS_PATH.parent.iterdir())):x}"


@dataclass
class DayGroup:
    label: str
    day: date
    events: list[Event]


def group_by_day(events: list[Event], today: date) -> list[DayGroup]:
    out = []
    for d, evs in groupby(sorted(events, key=lambda e: (e.start.date(), e.start)), key=lambda e: e.start.date()):
        out.append(DayGroup(_fmt_day(d, today), d, list(evs)))
    return out


def recommended(svc: "Service", *, days: int | None = None, min_score: int | None = None,
                start: datetime | None = None, end: datetime | None = None,
                keep: "Callable[[Event], bool] | None" = None) -> list[Event]:
    """Picks between start (default: now) and end (default: start + days, or the search horizon). `keep` narrows
    them (the dashboard's filters) before the report.max_events cap, so a narrow filter still finds its best events."""
    cfg = svc.cfg
    start = start or now(svc.tz)
    end = end or start + timedelta(days=days or cfg.search.horizon_days)
    ms = cfg.profile.min_score if min_score is None else min_score
    events = svc.db.upcoming_events(start, end, "status='ok' AND feedback >= 0 AND (score >= %s OR starred)", (ms,))
    # drop events that already ended today
    events = [e for e in events if (e.end or e.start) >= start - timedelta(hours=1) or not e.time_known]
    if keep is not None:
        events = [e for e in events if keep(e)]
    events.sort(key=lambda e: -(e.score or 0))
    events = events[: cfg.report.max_events]
    events.sort(key=lambda e: e.start)
    return events


def feed_url(svc: "Service", path: str) -> str:
    base = svc.cfg.report.public_base_url.rstrip("/")
    q = f"?{urlencode({'token': svc.cfg.report.feed_token})}" if svc.cfg.report.feed_token else ""
    return f"{base}{path}{q}"


def render_report_html(svc: "Service", events: list[Event], *, standalone: bool) -> str:
    today = now(svc.tz).date()
    tpl = env.get_template("email.html" if not standalone else "report_static.html")
    return tpl.render(
        groups=group_by_day(events, today),
        count=len(events),
        generated=now(svc.tz),
        cfg=svc.cfg,
        base_url=svc.cfg.report.public_base_url.rstrip("/"),
        ics_url=feed_url(svc, "/calendar.ics"),
        webcal_url=feed_url(svc, "/calendar.ics").replace("https://", "webcal://").replace("http://", "webcal://"),
    )


def render_text(events: list[Event], svc: "Service") -> str:
    today = now(svc.tz).date()
    lines = [f"EventScout picks ({len(events)})", ""]
    for g in group_by_day(events, today):
        lines.append(g.label.upper())
        for e in g.events:
            dist = f" · {e.distance_miles:.0f} mi" if e.distance_miles is not None else ""
            lines.append(f"  {_fmt_time(e)}  {e.title} — {e.location_display}{dist} · {e.cost_display} [{e.score}]")
            if e.url:
                lines.append(f"      {e.url}")
        lines.append("")
    lines.append(f"Dashboard: {svc.cfg.report.public_base_url}")
    return "\n".join(lines)


def build_outputs(svc: "Service", rl: "RunLog", *, send_email: bool = True) -> dict:
    cfg = svc.cfg
    out_dir = Path(cfg.report.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events = recommended(svc)
    starred = [e for e in svc.db.upcoming_events(now(svc.tz), now(svc.tz) + timedelta(days=365), "starred")]
    base = cfg.report.public_base_url

    ics_all = build_calendar(events, "EventScout picks", base)
    ics_starred = build_calendar(starred, "EventScout — going", base)
    (out_dir / "events.ics").write_bytes(ics_all)
    (out_dir / "starred.ics").write_bytes(ics_starred)
    (out_dir / "report.html").write_text(render_report_html(svc, events, standalone=True))
    (out_dir / "events.json").write_text(json.dumps([e.model_dump(mode="json") for e in events], indent=1))
    rl(f"report: {len(events)} recommended events written to {out_dir}")

    result = {"recommended": len(events), "emailed": False}
    if send_email and cfg.email.enabled:
        to_send = events
        if cfg.email.only_new:
            last = svc.db.get_kv("last_email_at")
            if last:
                cutoff = datetime.fromisoformat(last)
                to_send = [e for e in events if e.first_seen >= cutoff]
        if to_send:
            try:
                mailer.send_email(
                    cfg.email,
                    cfg.email.subject.format(count=len(to_send)),
                    render_report_html(svc, to_send, standalone=False),
                    render_text(to_send, svc),
                    [("eventscout.ics", build_calendar(to_send, "EventScout picks", base), "text/calendar")],
                )
                svc.db.set_kv("last_email_at", datetime.now().astimezone().isoformat())
                result["emailed"] = True
                rl(f"emailed {len(to_send)} events to {', '.join(cfg.email.recipients)}")
            except Exception as exc:  # noqa: BLE001
                rl(f"email failed: {type(exc).__name__}: {exc}", logging.WARNING)
        else:
            rl("email skipped: no new events")
    return result
