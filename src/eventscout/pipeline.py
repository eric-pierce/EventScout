"""The periodic run: fetch -> dedupe/store -> locate -> filter -> score -> enrich -> report."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from rapidfuzz import fuzz

from eventscout.config import Config
from eventscout.db import Database
from eventscout.dates import now
from eventscout.filtering import hard_filter
from eventscout.geo import Geo
from eventscout.http import BlockedError, Fetcher, FetchError
from eventscout.images import find_image
from eventscout.llm import LLM, LLMError, make_llm
from eventscout.models import Event, Source
from eventscout.sources import ADAPTERS, Context
from eventscout.sources.jsonld import parse_jsonld_events
from eventscout.tasks import extract_events, html_to_text, score_events, score_key

log = logging.getLogger("eventscout")


class RunLog:
    """Collects log lines for the run record while also emitting them to the process log."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, msg: str, level: int = logging.INFO) -> None:
        log.log(level, msg)
        with self._lock:
            self.lines.append(f"{datetime.now():%H:%M:%S} {msg}")

    def append(self, msg: str) -> None:  # list-like, for helpers that take a list
        self(msg)

    def text(self) -> str:
        return "\n".join(self.lines)


class Service:
    """Long-lived application state shared by the scheduler, CLI and web app."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.home.timezone)
        from eventscout.report import env

        env.globals["home_tz"] = self.tz
        self.db = Database(cfg.database, cfg.home.timezone)
        self.db.mark_stale_runs()
        self.run_lock = threading.Lock()
        self._llm: LLM | None = None
        self.sync_seed_sources()

    @property
    def llm(self) -> LLM:
        if self._llm is None:
            self._llm = make_llm(self.cfg)
        return self._llm

    def sync_seed_sources(self) -> None:
        # 'auto' seeds are resolved to a concrete kind on their first run (see _resolve_auto).
        for s in self.cfg.sources:
            self.db.upsert_seed_source(s.name, s.url, s.kind, s.options, s.enabled)
        for name in self.db.disable_removed_seeds({s.url for s in self.cfg.sources}):
            log.info("seed source %s is no longer in the config; disabled", name)

    def context(self, fetcher: Fetcher, days: int | None = None) -> Context:
        start = now(self.tz)
        return Context(
            cfg=self.cfg, fetcher=fetcher, llm_factory=lambda: self.llm,
            window_start=start, window_end=start + timedelta(days=days or self.cfg.search.horizon_days),
            db=self.db,
        )

    def close(self) -> None:
        self.db.close()

    @property
    def is_running(self) -> bool:
        return self.run_lock.locked()

    # ------------------------------------------------------------------
    def run(self, *, discover: bool = False, send_email: bool = True) -> dict:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("a run is already in progress")
        try:
            return self._run(discover=discover, send_email=send_email)
        finally:
            self.run_lock.release()

    def _run(self, *, discover: bool, send_email: bool) -> dict:
        from eventscout.report import build_outputs  # avoid import cycle

        cfg, db = self.cfg, self.db
        rl = RunLog()
        run_id = db.start_run("discover+run" if discover else "run")
        stats: dict = {}
        status = "ok"
        t0 = time.monotonic()
        fetcher = Fetcher(cfg.fetch)
        llm_usage_before = None
        try:
            ctx = self.context(fetcher)
            if self._llm is not None:
                llm_usage_before = self._llm.usage.as_dict()

            if discover and cfg.discovery.enabled:
                from eventscout.discovery import discover_sources

                try:
                    stats["discovery"] = discover_sources(db, self.llm, ctx, rl)
                    db.set_kv("last_discovery", now(self.tz).isoformat())
                except (LLMError, FetchError) as exc:
                    rl(f"discovery failed: {exc}", logging.WARNING)

            stats["fetch"] = self._fetch_all(ctx, rl)
            stats["locate"] = self._locate(ctx, rl)
            stats["filter"] = self._filter(ctx, rl)
            stats["score"] = self._score(ctx, rl)
            stats["enrich"] = self._enrich(ctx, rl)
            stats["images"] = self._images(ctx, rl)
            stats["sources"] = self._update_source_lifecycle(ctx, rl)
            db.prune_extractions()
            pruned = db.prune_events(now(self.tz) - timedelta(days=30))
            if pruned:
                rl(f"pruned {pruned} old events")
            stats["report"] = build_outputs(self, rl, send_email=send_email)
        except Exception as exc:  # record the failure, then re-raise for the caller/scheduler
            status = "error"
            rl(f"run failed: {type(exc).__name__}: {exc}", logging.ERROR)
            raise
        finally:
            fetcher.close()
            if self._llm is not None:
                usage = self._llm.usage.as_dict()
                if llm_usage_before:
                    usage = {k: usage[k] - llm_usage_before.get(k, 0) for k in usage}
                stats["llm"] = {"provider": self._llm.name, **usage}
            stats["seconds"] = round(time.monotonic() - t0, 1)
            db.finish_run(run_id, status, stats, rl.text())
        rl(f"run complete in {stats['seconds']}s")
        return stats

    # ------------------------------------------------------------------ fetch
    def _resolve_auto(self, src: Source, ctx: Context, rl: RunLog) -> Source:
        """Detect the concrete kind of an 'auto' source. The detected URL (e.g. a linked .ics feed) is kept in
        options._resolved_url so the configured URL stays the stable identity of the source."""
        if src.kind == "auto":
            from eventscout.detect import detect_kind

            det = detect_kind(src.url, ctx.fetcher, ctx)
            opts = {**src.options, "_resolved": True, "_resolved_url": det.url}
            self.db.update_source(src.id, kind=det.kind, options=opts)
            rl(f"{src.name}: detected as {det.kind} ({det.note or det.url})")
            src = src.model_copy(update={"kind": det.kind, "options": opts})
        if resolved := src.options.get("_resolved_url"):
            src = src.model_copy(update={"url": resolved})
        return src

    def _fetch_one(self, src: Source, ctx: Context, rl: RunLog) -> tuple[Source, list, str | None]:
        try:
            src = self._resolve_auto(src, ctx, rl)
            adapter = ADAPTERS.get(src.kind)
            if adapter is None:
                return src, [], f"unknown source kind {src.kind!r}"
            events = [e for e in adapter.fetch(src, ctx) if ctx.in_window(e)]
            return src, events, None
        except BlockedError as exc:
            return src, [], f"blocked: {exc}"
        except (FetchError, LLMError, ValueError, KeyError) as exc:
            return src, [], f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - one bad source must not sink the run
            log.exception("source %s crashed", src.name)
            return src, [], f"{type(exc).__name__}: {exc}"

    def _fetch_all(self, ctx: Context, rl: RunLog) -> dict:
        sources = self.db.list_sources(statuses=("active", "trial"))
        rl(f"fetching {len(sources)} sources")
        total, new_ids = 0, set()
        failures = 0
        with ThreadPoolExecutor(max_workers=self.cfg.fetch.concurrency) as pool:
            futures = [pool.submit(self._fetch_one, s, ctx, rl) for s in sources]
            for fut in as_completed(futures):
                src, events, err = fut.result()
                if err:
                    failures += 1
                    rl(f"  ✗ {src.name}: {err}", logging.WARNING)
                    self.db.record_source_result(src.id, 0, err)
                    continue
                for ev in events:
                    new_ids.add(self.db.upsert_event(ev, src))
                total += len(events)
                self.db.record_source_result(src.id, len(events), None)
                rl(f"  ✓ {src.name}: {len(events)} events")
        return {"sources": len(sources), "failed": failures, "events_seen": total, "unique_events": len(new_ids)}

    # ----------------------------------------------------------------- locate
    def _upcoming(self, ctx: Context, where: str = "", params: tuple = ()) -> list[Event]:
        return self.db.upcoming_events(ctx.window_start, ctx.window_end, where, params)

    def _locate(self, ctx: Context, rl: RunLog) -> dict:
        geo = Geo(self.cfg, self.db)
        todo = self._upcoming(ctx, "distance_miles IS NULL AND NOT geo_attempted AND NOT online")
        located = 0
        for ev in todo:
            fields = geo.locate(ev)
            self.db.update_event(ev.id, **fields)
            located += "distance_miles" in fields
        if todo:
            rl(f"located {located}/{len(todo)} events ({geo.lookups} geocoder lookups)")
        return {"attempted": len(todo), "located": located, "geocoder_lookups": geo.lookups}

    # ----------------------------------------------------------------- filter
    def _filter(self, ctx: Context, rl: RunLog) -> dict:
        counts = {"ok": 0, "filtered": 0}
        for ev in self._upcoming(ctx):
            reason = hard_filter(ev, self.cfg)
            new_status = "filtered" if reason else "ok"
            counts[new_status] += 1
            if new_status != ev.status or reason != ev.filter_reason:
                self.db.update_event(ev.id, status=new_status, filter_reason=reason)
        rl(f"filters: {counts['ok']} kept, {counts['filtered']} excluded by hard rules")
        return counts

    # ------------------------------------------------------------------ score
    def _score(self, ctx: Context, rl: RunLog) -> dict:
        profile = self.cfg.profile
        # New feedback steers scoring of new listings; `eventscout rescore` re-ranks everything with it.
        fp = profile.fingerprint()
        feedback = self.db.feedback_examples()
        events = self._upcoming(ctx, "status = 'ok'")
        need: list[tuple[Event, str]] = []
        cached = 0
        for ev in events:
            key = score_key(ev, fp)
            if ev.score_key == key and ev.score is not None:
                continue
            hit = self.db.get_cached_score(key)
            if hit:
                score, reason, tags = hit
                self.db.update_event(ev.id, score=score, score_reason=reason, tags=tags, score_key=key)
                cached += 1
            else:
                need.append((ev, key))
        if not need:
            rl(f"scoring: nothing new ({cached} reused from cache)")
            return {"scored": 0, "cached": cached}

        # Dedupe identical listings (e.g. weekly recurring shows) so each is scored once.
        unique: dict[str, list[Event]] = {}
        for ev, key in need:
            unique.setdefault(key, []).append(ev)
        reps = sorted(((evs[0], key) for key, evs in unique.items()), key=lambda r: r[0].start)
        cap = self.cfg.llm.max_scored_per_run
        deferred = max(0, len(reps) - cap)
        reps = reps[:cap]
        rl(f"scoring {len(reps)} unique listings ({len(need)} events, {cached} from cache) with {self.llm.name}"
           + (f"; {deferred} later listings deferred to the next run (llm.max_scored_per_run)" if deferred else ""))

        batch = self.cfg.llm.score_batch_size
        scored = errors = 0
        for i in range(0, len(reps), batch):
            chunk = reps[i : i + batch]
            try:
                results = score_events(self.llm, profile, [e for e, _ in chunk], feedback)
            except LLMError as exc:
                errors += 1
                rl(f"scoring batch failed: {exc}", logging.WARNING)
                if errors >= 3:
                    rl("too many scoring failures; stopping scoring for this run", logging.WARNING)
                    break
                continue
            for rep, key in chunk:
                if rep.id not in results:
                    continue
                score, reason, tags = results[rep.id]
                self.db.put_cached_score(key, score, reason, tags)
                for ev in unique[key]:
                    self.db.update_event(ev.id, score=score, score_reason=reason, tags=tags, score_key=key)
                    scored += 1
        return {"scored": scored, "cached": cached, "deferred": deferred,
                "llm_batches": (len(reps) + batch - 1) // batch, "errors": errors}

    # ----------------------------------------------------------------- enrich
    def _enrich(self, ctx: Context, rl: RunLog) -> dict:
        """Visit detail pages of top events missing a start time or price to fill the gaps."""
        limit = self.cfg.report.enrich_per_run
        if limit <= 0:
            return {"enriched": 0}
        cands = self._upcoming(
            ctx,
            "status='ok' AND NOT enriched AND score >= %s AND url IS NOT NULL AND (NOT time_known OR (price_min IS NULL AND is_free IS NULL))",
            (self.cfg.profile.min_score,),
        )
        cands.sort(key=lambda e: -(e.score or 0))
        done = 0
        for ev in cands[:limit]:
            fields: dict = {"enriched": True}
            try:
                res = ctx.fetcher.get(ev.url)
                if ev.image_url is None and res.is_html and (img := find_image(res.text, res.url)):
                    fields["image_url"] = img
                fields["image_checked"] = True
                found = parse_jsonld_events(res.text, res.url, ctx.tz)
                if not found and res.is_html:
                    text = html_to_text(res.text, res.url)[: self.cfg.llm.extract_chunk_chars]
                    found = extract_events(self.llm, self.cfg, text, res.url, now(self.tz))
                best = max(found, key=lambda f: fuzz.token_set_ratio(f.title.lower(), ev.title.lower()), default=None)
                if best and fuzz.token_set_ratio(best.title.lower(), ev.title.lower()) >= 70 and best.start.date() == ev.start.date():
                    if not ev.time_known and best.time_known:
                        fields.update(start=best.start, time_known=True, all_day=False)
                    if best.end and not ev.end:
                        fields["end"] = best.end
                    for f in ("price_min", "price_max", "is_free", "cost_text", "address", "venue_name"):
                        if getattr(ev, f) is None and getattr(best, f) is not None:
                            fields[f] = getattr(best, f)
                    if len(best.description) > len(ev.description):
                        fields["description"] = best.description
                    done += 1
            except (FetchError, LLMError) as exc:
                log.info("enrich %s failed: %s", ev.url, exc)
            except Exception as exc:  # noqa: BLE001 - a malformed third-party page must not sink the run
                log.warning("enrich %s failed unexpectedly: %s: %s", ev.url, type(exc).__name__, exc)
            self.db.update_event(ev.id, **fields)
        if cands:
            rl(f"enriched {done}/{min(limit, len(cands))} top events from detail pages")
        return {"enriched": done}

    # ----------------------------------------------------------------- images
    def _images(self, ctx: Context, rl: RunLog) -> dict:
        """Give picks a thumbnail: the preview image (og:image etc.) of the event's own page. Linked, not copied."""
        limit = self.cfg.report.images_per_run
        if limit <= 0:
            return {"found": 0}
        cands = self._upcoming(
            ctx,
            "status='ok' AND image_url IS NULL AND NOT image_checked AND url IS NOT NULL AND (score >= %s OR starred)",
            (self.cfg.profile.min_score,),
        )
        cands.sort(key=lambda e: -(e.score or 0))
        cands = cands[:limit]
        by_url: dict[str, list[Event]] = {}
        for ev in cands:  # several events can share a listing page; fetch each page once
            by_url.setdefault(ev.url, []).append(ev)

        def look(url: str) -> str | None:
            try:
                res = ctx.fetcher.get(url)
                return find_image(res.text, res.url) if res.is_html else None
            except FetchError as exc:
                log.info("image lookup %s failed: %s", url, exc)
                return None
            except Exception as exc:  # noqa: BLE001 - same: skip this page, keep the run going
                log.warning("image lookup %s failed unexpectedly: %s: %s", url, type(exc).__name__, exc)
                return None

        found = 0
        with ThreadPoolExecutor(max_workers=self.cfg.fetch.concurrency) as pool:
            for url, img in zip(by_url, pool.map(look, by_url)):
                for ev in by_url[url]:
                    self.db.update_event(ev.id, image_checked=True, **({"image_url": img} if img else {}))
                    found += bool(img)
        dropped = self.db.drop_generic_images()
        if cands:
            rl(f"images: found {found}/{len(cands)} from event pages"
               + (f"; dropped {dropped} generic site images" if dropped else ""))
        return {"checked": len(cands), "found": found, "generic_dropped": dropped}

    # -------------------------------------------------------------- lifecycle
    def _update_source_lifecycle(self, ctx: Context, rl: RunLog) -> dict:
        min_score = self.cfg.profile.min_score
        relevant: dict[int, int] = {}
        for ev in self._upcoming(ctx, "status='ok' AND score >= %s", (min_score,)):
            for sid in ev.source_ids:
                relevant[sid] = relevant.get(sid, 0) + 1
        changes = {"promoted": 0, "retired": 0, "disabled": 0}
        for src in self.db.list_sources(statuses=("active", "trial")):
            count = relevant.get(src.id, 0)
            self.db.update_source(src.id, relevant_events=count)
            if src.status == "trial":
                if count > 0:
                    self.db.update_source(src.id, status="active")
                    changes["promoted"] += 1
                    rl(f"source promoted to active: {src.name} ({count} relevant events)")
                elif src.runs >= self.cfg.discovery.trial_runs:
                    self.db.update_source(src.id, status="retired")
                    changes["retired"] += 1
                    rl(f"source retired after trial with no relevant events: {src.name}")
            if src.consecutive_failures >= 5:
                self.db.update_source(src.id, status="disabled")
                changes["disabled"] += 1
                rl(f"source disabled after 5 consecutive failures: {src.name} ({src.last_error})", logging.WARNING)
        return changes
