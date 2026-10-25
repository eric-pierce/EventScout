"""Find new event sources: web research (Claude web search or SearXNG) -> LLM shortlist -> validate -> add as trial."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from eventscout.db import Database, domain_of
from eventscout.detect import Detection, detect_kind, trial_fetch
from eventscout.http import FetchError
from eventscout.llm import LLM, LLMError
from eventscout.models import RawEvent
from eventscout.sources import Context
from eventscout.tasks import DISCOVER_SYSTEM, discovery_brief, pick_candidates

log = logging.getLogger(__name__)


def _searxng_material(ctx: Context, focus: str | None = None) -> str:
    cfg = ctx.cfg
    if not cfg.discovery.searxng_url:
        raise LLMError("discovery needs Claude web search or a SearXNG instance (discovery.searxng_url)")
    home = cfg.home
    area = home.nearby[0] if home.nearby else home.city
    month = f" {ctx.window_start:%B %Y}" if focus else ""
    queries = [f"{home.city} {home.region} events calendar{month}", f"things to do in {area}{month or ' this weekend'}"]
    for interest in sorted(cfg.profile.interests, key=lambda i: -i.weight)[:8]:
        queries.append(f"{interest.topic} events calendar {area} {home.region}")
        queries.append(f"{interest.topic} {home.city} upcoming events{month}")
    lines: list[str] = []
    seen: set[str] = set()
    with httpx.Client(timeout=30) as client:
        for q in queries:
            try:
                resp = client.get(cfg.discovery.searxng_url.rstrip("/") + "/search", params={"q": q, "format": "json"})
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("searxng query %r failed: %s", q, exc)
                continue
            for r in results[:10]:
                url = r.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                lines.append(f"- {r.get('title', '')} <{url}>\n  {r.get('content', '')[:240]}")
    if not lines:
        raise LLMError("SearXNG returned no results (is JSON output enabled in settings.yml?)")
    return "\n".join(lines)


def _claude_material(llm: LLM, ctx: Context, known: set[str], focus: str | None = None) -> str:
    user = (
        discovery_brief(ctx.cfg, known, focus)
        + f"\n\nSearch the web for up to {ctx.cfg.discovery.max_candidates_per_run * 2} event-listing pages matching these "
          "interests and area that are not already tracked. Mix broad local calendars with niche sources for the "
          "highest-weight interests. For each, give the exact listing-page URL and one line on what it covers."
    )
    return llm.research(system=DISCOVER_SYSTEM, user=user)


@dataclass
class FoundSource:
    name: str
    why: str
    det: Detection
    events: list[RawEvent]  # what the validation fetch found inside the window


def find_sources(llm: LLM, ctx: Context, known: set[str], log_lines: list[str], *,
                 focus: str | None = None) -> tuple[list[FoundSource], dict]:
    """Search for event-listing pages (Claude web search or SearXNG), shortlist them with the LLM, and keep those
    that actually yield enough events in the window. Nothing is stored. `focus` adds context such as trip dates."""
    cfg = ctx.cfg
    backend = cfg.discovery.search_backend
    if backend == "auto":
        backend = "claude" if llm.supports_web_search else "searxng"
    known = set(known)
    blocked = {d.lower() for d in cfg.discovery.blocked_domains}

    material = _claude_material(llm, ctx, known, focus) if backend == "claude" else _searxng_material(ctx, focus)
    candidates = pick_candidates(llm, cfg, material, known, focus)
    log_lines.append(f"discovery: {len(candidates)} candidates via {backend}")

    found: list[FoundSource] = []
    rejected = 0
    for cand in candidates[: cfg.discovery.max_candidates_per_run]:
        url = cand.get("url", "").strip()
        if not url.startswith("http"):
            continue
        dom = domain_of(url)
        if dom in known or any(dom == b or dom.endswith("." + b) for b in blocked):
            continue
        name = cand.get("name") or urlparse(url).netloc
        try:
            det = detect_kind(url, ctx.fetcher, ctx)
            events = trial_fetch(det, name, ctx)
        except (FetchError, LLMError, ValueError) as exc:
            rejected += 1
            log_lines.append(f"discovery: rejected {url}: {exc}")
            continue
        if len(events) < cfg.discovery.min_events_to_accept:
            rejected += 1
            log_lines.append(f"discovery: rejected {url}: only {len(events)} events in the window")
            continue
        known.add(dom)
        found.append(FoundSource(name, cand.get("why", ""), det, events))
        log_lines.append(f"discovery: found {name} ({det.kind}, {len(events)} events) {det.url}")
    return found, {"candidates": len(candidates), "found": len(found), "rejected": rejected}


def discover_sources(db: Database, llm: LLM, ctx: Context, log_lines: list[str]) -> dict:
    """Home discovery: add validated finds as trial sources (promoted or retired by later runs)."""
    found, stats = find_sources(llm, ctx, db.known_domains(), log_lines)
    added = 0
    for f in found:
        sid = db.add_source(f.name, f.det.url, f.det.kind, status="trial", origin="discovered",
                            notes=f"{f.why} [{f.det.note}; {len(f.events)} events at discovery]")
        added += bool(sid)
    return {"candidates": stats["candidates"], "added": added, "rejected": stats["rejected"]}
