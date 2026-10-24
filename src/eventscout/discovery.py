"""Find new event sources: web research (Claude web search or SearXNG) -> LLM shortlist -> validate -> add as trial."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from eventscout.db import Database, domain_of
from eventscout.detect import detect_kind, trial_fetch
from eventscout.http import FetchError
from eventscout.llm import LLM, LLMError
from eventscout.sources import Context
from eventscout.tasks import DISCOVER_SYSTEM, discovery_brief, pick_candidates

log = logging.getLogger(__name__)


def _searxng_material(ctx: Context) -> str:
    cfg = ctx.cfg
    if not cfg.discovery.searxng_url:
        raise LLMError("discovery needs Claude web search or a SearXNG instance (discovery.searxng_url)")
    home = cfg.home
    places = [home.city] + home.nearby[:3]
    queries = [f"{home.city} {home.region} events calendar", f"things to do this weekend {places[1]}"]
    for interest in sorted(cfg.profile.interests, key=lambda i: -i.weight)[:8]:
        queries.append(f"{interest.topic} events calendar {places[1]} {home.region}")
        queries.append(f"{interest.topic} {home.city} upcoming events")
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


def _claude_material(llm: LLM, ctx: Context, known: set[str]) -> str:
    user = (
        discovery_brief(ctx.cfg, known)
        + f"\n\nSearch the web for up to {ctx.cfg.discovery.max_candidates_per_run * 2} event-listing pages matching these "
          "interests and area that are not already tracked. Mix broad local calendars with niche sources for the "
          "highest-weight interests. For each, give the exact listing-page URL and one line on what it covers."
    )
    return llm.research(system=DISCOVER_SYSTEM, user=user)


def discover_sources(db: Database, llm: LLM, ctx: Context, log_lines: list[str]) -> dict:
    cfg = ctx.cfg
    backend = cfg.discovery.search_backend
    if backend == "auto":
        backend = "claude" if llm.supports_web_search else "searxng"
    known = db.known_domains()
    blocked = {d.lower() for d in cfg.discovery.blocked_domains}

    if backend == "claude":
        material = _claude_material(llm, ctx, known)
    else:
        material = _searxng_material(ctx)
    candidates = pick_candidates(llm, cfg, material, known)
    log_lines.append(f"discovery: {len(candidates)} candidates via {backend}")

    added, rejected = [], []
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
            rejected.append(f"{url}: {exc}")
            continue
        if len(events) < cfg.discovery.min_events_to_accept:
            rejected.append(f"{url}: only {len(events)} upcoming events")
            continue
        sid = db.add_source(name, det.url, det.kind, status="trial", origin="discovered",
                            notes=f"{cand.get('why', '')} [{det.note}; {len(events)} events at discovery]")
        if sid:
            known.add(dom)
            added.append(f"{name} ({det.kind}) {det.url}")
    for a in added:
        log_lines.append(f"discovery: added trial source {a}")
    for r in rejected:
        log_lines.append(f"discovery: rejected {r}")
    return {"candidates": len(candidates), "added": len(added), "rejected": len(rejected)}
