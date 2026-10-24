"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from eventscout.config import load_config


def _service(args):
    from eventscout.pipeline import Service

    return Service(load_config(args.config))


def cmd_serve(args) -> None:
    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    from eventscout.web.app import create_app

    from eventscout.filesecrets import secrets_report

    svc = _service(args)
    cfg = svc.cfg
    log = logging.getLogger("eventscout")
    log.info("secrets: %s", ", ".join(f"{k}={v}" for k, v in secrets_report().items()))  # sources only, never values

    def scheduled_run(discover: bool = False) -> None:
        if svc.is_running:
            log.info("skipping scheduled run: one is already in progress")
            return
        try:
            svc.run(discover=discover)
        except Exception:  # noqa: BLE001
            log.exception("scheduled run failed")

    sched = BackgroundScheduler(timezone=cfg.home.timezone)
    sched.add_job(scheduled_run, CronTrigger.from_crontab(cfg.schedule.run, timezone=cfg.home.timezone),
                  id="run", max_instances=1, coalesce=True, misfire_grace_time=3600)
    if cfg.discovery.enabled:
        sched.add_job(scheduled_run, CronTrigger.from_crontab(cfg.discovery.schedule, timezone=cfg.home.timezone),
                      kwargs={"discover": True}, id="discover", max_instances=1, coalesce=True, misfire_grace_time=3600)
    if cfg.schedule.run_on_startup:
        sched.add_job(scheduled_run, id="startup", next_run_time=datetime.now().astimezone())
    sched.start()
    for job in sched.get_jobs():
        log.info("scheduled %s: next run %s", job.id, job.next_run_time)

    uvicorn.run(create_app(svc), host=cfg.web.host, port=cfg.web.port, log_level="info", access_log=False)
    sched.shutdown(wait=False)


def cmd_run(args) -> None:
    svc = _service(args)
    stats = svc.run(discover=args.discover, send_email=not args.no_email)
    print(json.dumps(stats, indent=2, default=str))


def cmd_discover(args) -> None:
    from eventscout.discovery import discover_sources
    from eventscout.http import Fetcher

    svc = _service(args)
    lines: list[str] = []
    fetcher = Fetcher(svc.cfg.fetch)
    try:
        stats = discover_sources(svc.db, svc.llm, svc.context(fetcher), lines)
    finally:
        fetcher.close()
    print("\n".join(lines))
    print(json.dumps({**stats, "llm": svc.llm.usage.as_dict()}, indent=2))


def cmd_report(args) -> None:
    from eventscout.pipeline import RunLog
    from eventscout.report import build_outputs

    svc = _service(args)
    print(json.dumps(build_outputs(svc, RunLog(), send_email=args.email), indent=2))


def cmd_sources(args) -> None:
    from eventscout.detect import detect_kind, trial_fetch
    from eventscout.http import Fetcher
    from eventscout.sources import ADAPTERS

    svc = _service(args)
    if args.action == "list":
        for s in svc.db.list_sources():
            err = f"  ! {s.last_error}" if s.last_error else ""
            print(f"[{s.id:>3}] {s.status:<8} {s.kind:<12} {s.name:<32} {s.last_event_count:>4} ev  {s.url}{err}")
        return
    fetcher = Fetcher(svc.cfg.fetch)
    ctx = svc.context(fetcher)
    try:
        if args.action == "add":
            det = detect_kind(args.target, fetcher, ctx)
            events = trial_fetch(det, args.name or args.target, ctx)
            sid = svc.db.add_source(args.name or args.target, det.url, det.kind, "active", "manual", notes=det.note)
            print(f"added #{sid}: {det.kind} ({det.note}) — {len(events)} upcoming events")
        elif args.action == "check":
            targets = svc.db.list_sources() if args.target == "all" else [svc.db.get_source(int(args.target))]
            for src in targets:
                if src is None:
                    continue
                try:
                    src = svc._resolve_auto(src, ctx, lambda *a, **k: None)
                    evs = [e for e in ADAPTERS[src.kind].fetch(src, ctx) if ctx.in_window(e)]
                    print(f"✓ {src.name} ({src.kind}): {len(evs)} events in window")
                    for e in evs[: args.show]:
                        print(f"    {e.start:%a %b %d %H:%M}{'' if e.time_known else ' (no time)'}  {e.title[:70]}  @ {e.venue_name or e.city or '?'}  [{e.cost_text or e.price_min or ('free' if e.is_free else '?')}]")
                except Exception as exc:  # noqa: BLE001
                    print(f"✗ {src.name} ({src.kind}): {type(exc).__name__}: {exc}")
    finally:
        fetcher.close()


def cmd_rescore(args) -> None:
    svc = _service(args)
    n = svc.db.clear_scores()
    print(f"cleared scores on {n} events; they will be re-scored (with your latest feedback) on the next run")


def cmd_test_llm(args) -> None:
    from eventscout.tasks import EXTRACT_SCHEMA, EXTRACT_SYSTEM

    svc = _service(args)
    sample = "Saturday October 3, 8pm — The Lowdown Brass Band at Club Passim, 47 Palmer St, Cambridge. $20 advance. <https://passim.org/e/1>"
    data = svc.llm.json(system=EXTRACT_SYSTEM, user=f"Today is {datetime.now():%A, %B %d, %Y}.\n<page>\n{sample}\n</page>",
                        schema=EXTRACT_SCHEMA, task="extract")
    print(json.dumps(data, indent=2))
    print(svc.llm.usage.as_dict())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="eventscout", description="Self-hosted local event finder")
    parser.add_argument("-c", "--config", help="path to config.yaml (default: $EVENTSCOUT_CONFIG or /config/config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="web dashboard + scheduler").set_defaults(fn=cmd_serve)

    p = sub.add_parser("run", help="run the pipeline once")
    p.add_argument("--discover", action="store_true", help="also search for new sources first")
    p.add_argument("--no-email", action="store_true")
    p.set_defaults(fn=cmd_run)

    sub.add_parser("discover", help="search for new sources only").set_defaults(fn=cmd_discover)

    p = sub.add_parser("report", help="regenerate report outputs from stored data")
    p.add_argument("--email", action="store_true", help="also send the email digest")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("sources", help="list / add / check sources")
    p.add_argument("action", choices=["list", "add", "check"])
    p.add_argument("target", nargs="?", default="all", help="URL for add; source id or 'all' for check")
    p.add_argument("--name")
    p.add_argument("--show", type=int, default=5, help="events to print per source for check")
    p.set_defaults(fn=cmd_sources)

    sub.add_parser("rescore", help="re-score all events on the next run (after lots of feedback)").set_defaults(fn=cmd_rescore)

    sub.add_parser("test-llm", help="sanity-check the configured LLM provider").set_defaults(fn=cmd_test_llm)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "anthropic", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from eventscout.filesecrets import SecretFileError

    try:
        args.fn(args)
    except SecretFileError as exc:
        sys.exit(f"eventscout: secrets: {exc}")
