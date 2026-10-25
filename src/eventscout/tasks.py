"""LLM-backed tasks: event extraction from arbitrary pages, interest scoring, source-candidate selection."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import NamedTuple
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process, utils

from eventscout.config import Config, Profile
from eventscout.dates import parse_when
from eventscout.db import normalize_title
from eventscout.llm import LLM
from eventscout.models import Event, RawEvent
from eventscout.urls import safe_urljoin

log = logging.getLogger(__name__)


def _nullable(t: str) -> dict:
    return {"type": [t, "null"]}


# ---------------------------------------------------------------------------
# HTML -> text (keeping link targets so the LLM can attribute event URLs)
# ---------------------------------------------------------------------------

def html_to_text(html: str, base_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form", "header", "footer", "nav"]):
        tag.decompose()
    for a in soup.find_all("a", href=True):
        href = safe_urljoin(base_url, a["href"])  # None for malformed links like "http://[broken"
        label = a.get_text(" ", strip=True)
        if label and href and href.startswith("http"):
            a.replace_with(f"{label} <{href}>")
    for br in soup.find_all(["br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "article", "section"]):
        br.insert_after("\n")
    text = soup.get_text(" ")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            nl = text.rfind("\n", start + size // 2, end)
            if nl > 0:
                end = nl
        chunks.append(text[start:end])
        start = end
    return chunks


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO 8601 local datetime, or YYYY-MM-DD if no time is given"},
                    "end": _nullable("string"),
                    "all_day": {"type": "boolean"},
                    "venue_name": _nullable("string"),
                    "address": _nullable("string"),
                    "city": _nullable("string"),
                    "price_min": _nullable("number"),
                    "price_max": _nullable("number"),
                    "is_free": _nullable("boolean"),
                    "cost_text": _nullable("string"),
                    "url": _nullable("string"),
                    "description": {"type": "string"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                    "online": {"type": "boolean"},
                },
                "required": [
                    "title", "start", "end", "all_day", "venue_name", "address", "city", "price_min", "price_max",
                    "is_free", "cost_text", "url", "description", "categories", "online",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["events"],
    "additionalProperties": False,
}

EXTRACT_SYSTEM = """You extract event listings from web page text for a personal local-events digest.

Return every distinct, specific event on the page that has a date. Rules:
- Only real events with a concrete date (skip navigation, ads, "subscribe" blurbs, past-event archives, venue descriptions).
- Skip standing promotions and long runs listed as "Through <date>" or "Every <day>" deals (happy hours, pop-ups, specials, weeks-long exhibitions or classes) unless the page gives a specific dated occurrence worth attending.
- A recurring listing ("Every Tuesday") becomes one entry per concrete date shown on the page; if no concrete dates are shown, emit only the next occurrence.
- start/end: ISO 8601 local time without offset (e.g. 2026-10-03T19:30). Use YYYY-MM-DD for start when no time is given. Infer the year from context and today's date; events are upcoming, not past.
- Prices in the page's currency as numbers. is_free=true only when the page says free/no cover. cost_text keeps the original wording (e.g. "$15 adv / $20 door").
- url: the link most specific to the event (links appear as "label <url>"). null if none.
- description: one or two plain sentences, at most 300 characters.
- categories: 1-4 short lowercase genre/topic words (e.g. "jazz", "comedy", "food festival", "tech meetup").
- online=true only for virtual-only events.
- Never invent details that are not on the page; use null instead."""


def _raw_from_llm(item: dict, tz: ZoneInfo, page_url: str) -> RawEvent | None:
    parsed = parse_when(item.get("start"), tz)
    if not parsed or not item.get("title"):
        return None
    start, known = parsed
    end = parse_when(item.get("end"), tz)
    url = item.get("url")
    if url:
        url = safe_urljoin(page_url, url)
    return RawEvent(
        title=item["title"].strip()[:300],
        start=start,
        time_known=known and not item.get("all_day", False),
        all_day=bool(item.get("all_day")),
        end=end[0] if end and end[0] > start else None,
        venue_name=item.get("venue_name"),
        address=item.get("address"),
        city=item.get("city"),
        price_min=item.get("price_min"),
        price_max=item.get("price_max"),
        is_free=item.get("is_free"),
        cost_text=item.get("cost_text"),
        url=url or page_url,
        description=(item.get("description") or "")[:600],
        categories=[c.lower() for c in item.get("categories") or []][:6],
        online=bool(item.get("online")),
    )


def extract_events(llm: LLM, cfg: Config, text: str, page_url: str, today: datetime, db=None) -> list[RawEvent]:
    """LLM extraction, chunked. With `db`, each chunk's result is cached by content hash, so unchanged
    pages cost nothing on later runs."""
    tz = ZoneInfo(cfg.home.timezone)
    chunks = chunk_text(text, cfg.llm.extract_chunk_chars)
    limit = cfg.fetch.max_llm_chunks_per_page
    if len(chunks) > limit:
        log.warning("%s: page text is %d chunks; extracting the first %d (raise fetch.max_llm_chunks_per_page to read more)",
                    page_url, len(chunks), limit)
        chunks = chunks[:limit]
    events: list[RawEvent] = []
    for i, chunk in enumerate(chunks, 1):
        key = hashlib.sha1(f"{page_url}|{chunk}".encode()).hexdigest()
        items = db.get_extraction(key) if db is not None else None
        if items is None:
            user = (
                f"Today is {today:%A, %B %d, %Y}. Timezone: {cfg.home.timezone}. Region: {cfg.home.label}.\n"
                f"Page URL: {page_url}\n(Part {i} of {len(chunks)})\n\n<page>\n{chunk}\n</page>"
            )
            items = llm.json(system=EXTRACT_SYSTEM, user=user, schema=EXTRACT_SCHEMA, task="extract").get("events", [])
            if db is not None:
                db.put_extraction(key, items)
        chunk_events = [ev for item in items if (ev := _raw_from_llm(item, tz, page_url))]
        attach_event_links(chunk_events, chunk, page_url)
        events += chunk_events
    return events


_LINK = re.compile(r"([^\n<>]{2,200}?) <(https?://[^>\s]+)>")


_TITLE_THEN_SEP = re.compile(r"\s*[@|:\u2013\u2014-]\s")


def _norm_url(url: str | None) -> str:
    return (url or "").split("#", 1)[0].rstrip("/")


def _time_pattern(ev: RawEvent) -> re.Pattern:
    """Matches the event's start time as listings print it: 8:00PM, 8 pm, 8pm, 20:00."""
    h, m = ev.start.hour, ev.start.minute
    h12, ap = h % 12 or 12, "a" if h < 12 else "p"
    mins = f"(?::{m:02d})?" if m == 0 else f":{m:02d}"
    return re.compile(rf"(?<!\d)(?:{h12}{mins}\s*{ap}\.?m\b|{h:02d}:{m:02d}(?!\d))", re.I)


def attach_event_links(events: list[RawEvent], text: str, page_url: str) -> None:
    """Make sure each event links to its own page, not the listing it was read from.

    An event whose URL is missing, is the listing page itself, or doesn't appear on the page (a made-up link)
    gets the page link whose anchor text best matches its title. Repeated titles (a 2pm and an 8pm show)
    take the page's links in start-time order. If nothing matches, the event keeps the listing page URL.
    """
    # (label, url, context): context is the text after the link, which on listing pages holds the venue and time.
    # It ends at the next listing with the same label, so a 2pm entry's context never reaches the 8pm entry's time.
    matches = list(_LINK.finditer(text))
    links = []
    for i, m in enumerate(matches):
        label, stop = m.group(1).strip(), m.end() + 200
        for nxt in matches[i + 1:]:
            if nxt.start() >= stop:
                break
            if fuzz.token_sort_ratio(label, nxt.group(1), processor=utils.default_process) >= 90:
                stop = nxt.start()
                break
        links.append((label, m.group(2), text[m.end():stop]))
    on_page = {_norm_url(u) for _, u, _ in links}
    listing = _norm_url(page_url)
    used: set[str] = set()
    for ev in sorted(events, key=lambda e: e.start):
        own = _norm_url(ev.url)
        if own and own != listing and own in on_page:
            used.add(own)
            continue
        title = ev.title.lower()
        when = _time_pattern(ev) if ev.time_known else None
        best, best_score = None, 0.0
        for label, url, after in links:
            nurl = _norm_url(url)
            if nurl in used or nurl == listing:
                continue
            # Whole-title comparison (word order and punctuation ignored). A label that is merely a subset of the
            # title, like a venue or city link, must not count; only labels the site visibly truncated with "..."
            # may match a prefix of the title.
            score = fuzz.token_sort_ratio(title, label, processor=utils.default_process)
            if label.rstrip().endswith(("...", "…")):
                stem = label.rstrip(" .…")
                if len(stem) >= 12:
                    score = max(score, fuzz.partial_ratio(stem, title, processor=utils.default_process))
            # Newsletter style "Title @ Venue @ 6PM" / "Title - Venue": the whole title, then a separator.
            if len(title) >= 8 and _TITLE_THEN_SEP.match(label.lower().removeprefix(title)) and label.lower().startswith(title):
                score = max(score, 95)
            if when and when.search(after):  # same title listed several times: prefer the showing at this time
                score += 5
            if score > best_score:
                best, best_score = url, score
        if best and best_score >= 85:
            ev.url = best
            used.add(_norm_url(best))
        elif own not in on_page:
            ev.url = page_url


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer", "description": "0-100"},
                    "reason": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    "against": {"type": "array", "items": {"type": "string"}},
                    "factors": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "score", "reason", "tags", "matched", "against", "factors"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def profile_text(profile: Profile) -> str:
    lines = ["Interests (weight 1-5, 5 = love it):"]
    lines += [f"- {i.topic} (weight {i.weight})" for i in sorted(profile.interests, key=lambda i: -i.weight)]
    if profile.dislikes:
        lines.append("Not interested in:")
        lines += [f"- {d}" for d in profile.dislikes]
    if profile.notes.strip():
        lines.append("Other preferences:\n" + profile.notes.strip())
    return "\n".join(lines)


def feedback_text(feedback: list[dict]) -> str:
    liked = [f"{f['title']} @ {f['venue'] or '?'}" for f in feedback if f["value"] > 0][:20]
    disliked = [f"{f['title']} @ {f['venue'] or '?'}" for f in feedback if f["value"] < 0][:20]
    fb = ""
    if liked:
        fb += "Events they liked:\n" + "\n".join(f"+ {x}" for x in liked) + "\n"
    if disliked:
        fb += "Events they marked not interested:\n" + "\n".join(f"- {x}" for x in disliked) + "\n"
    return fb or "No feedback yet.\n"


def score_system(profile: Profile, feedback: list[dict] | None = None) -> str:
    """The scoring instructions, profile and feedback examples. Identical for every batch in a run, so the whole
    thing is cached after the first batch (the events to rate go in the user message)."""
    return f"""You rate local events for one person based on how likely they are to want to attend.

{profile_text(profile)}

Their past feedback:
{feedback_text(feedback or [])}

Scoring guide:
- 85-100: squarely matches a high-weight interest and is a notable/appealing instance of it.
- 65-84: good match for an interest, or a strong one-off event they'd plausibly enjoy.
- 40-64: loosely related or generic.
- 0-39: unrelated, matches a dislike, aimed at a different audience (e.g. children's programming, professional-only), or low-effort filler.
Use the person's past feedback as the strongest signal of taste. Consider timing, distance and cost against their stated preferences.
reason: at most 18 words, specific to the event (not "matches interests").
tags: 1-3 short lowercase labels drawn from their interest list where applicable.

Also explain each score so the person can tune their preferences:
matched: the interests from their list that this event fits, copied exactly as written above (empty if none).
against: what lowered the score. Copy the exact "not interested in" item or preference text when one applies;
  otherwise a short phrase such as "aimed at families", "22 mi away" or "pricey for a weeknight". Empty if nothing.
factors: up to 3 short phrases on anything else that mattered: timing, distance, cost, or similar events they
  liked or hid."""


def score_key(ev: Event, profile_fp: str) -> str:
    """Cache key: identical listings (e.g. weekly recurring shows) share one score."""
    blob = "|".join([
        normalize_title(ev.title),
        (ev.venue_name or "").lower(),
        ev.description[:300].lower(),
        ev.cost_display,
        ev.start.strftime("%a %p"),
        profile_fp,
    ])
    return hashlib.sha1(blob.encode()).hexdigest()


def _event_brief(ev: Event) -> dict:
    d = {
        "id": ev.id,
        "title": ev.title,
        "when": ev.start.strftime("%a %b %d") + ("" if not ev.time_known else ev.start.strftime(" %-I:%M%p")),
        "where": ev.location_display,
        "cost": ev.cost_display,
    }
    if ev.distance_miles is not None:
        d["miles"] = round(ev.distance_miles, 1)
    if ev.categories:
        d["categories"] = ev.categories[:4]
    if ev.description:
        d["about"] = ev.description[:350]
    return d


class Score(NamedTuple):
    score: int
    reason: str
    tags: list[str]
    detail: dict  # {"matched": [profile interest topics], "against": [...], "factors": [...]}


def _explanation(r: dict, profile: Profile) -> dict:
    """The model's breakdown, with `matched` mapped onto the profile's actual interest topics. Anything that isn't
    one of the person's interests is dropped, so the UI can always show its weight and link it to Settings."""
    topics = [i.topic for i in profile.interests]
    matched: list[str] = []
    for m in r.get("matched") or []:
        hit = process.extractOne(str(m), topics, scorer=fuzz.token_sort_ratio, processor=utils.default_process,
                                 score_cutoff=88) if topics else None
        if hit and hit[0] not in matched:
            matched.append(hit[0])
    short = lambda xs: [str(x).strip()[:90] for x in (xs or []) if str(x).strip()][:3]  # noqa: E731
    return {"matched": matched, "against": short(r.get("against")), "factors": short(r.get("factors"))}


def score_events(llm: LLM, profile: Profile, events: list[Event], feedback: list[dict]) -> dict[int, Score]:
    user = "Rate each event:\n" + json.dumps([_event_brief(e) for e in events], ensure_ascii=False)
    data = llm.json(system=score_system(profile, feedback), user=user, schema=SCORE_SCHEMA, task="score")
    wanted = {e.id for e in events}
    out: dict[int, Score] = {}
    for r in data.get("results", []):
        if r.get("id") in wanted:
            out[r["id"]] = Score(max(0, min(100, int(r["score"]))), r.get("reason", "")[:200],
                                 [t.lower() for t in r.get("tags", [])][:4], _explanation(r, profile))
    missing = wanted - out.keys()
    if missing:
        log.warning("scorer skipped %d events", len(missing))
    return out


# ---------------------------------------------------------------------------
# Source discovery
# ---------------------------------------------------------------------------

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["name", "url", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

DISCOVER_SYSTEM = """You find recurring sources of local event listings for a personal event digest.

Good sources are pages that list MANY upcoming dated events and are updated continually: city/town event calendars,
venue calendars, neighborhood aggregators, university or museum calendars, club/meetup calendars, iCal feeds, niche
newsletters with web calendars. Prefer the calendar/listing page URL itself, not a single event, article or homepage.
Avoid social networks, ticket resale sites, and national directories without local filtering."""


def discovery_brief(cfg: Config, known_domains: set[str], focus: str | None = None) -> str:
    nearby = f" (also consider: {', '.join(cfg.home.nearby)})" if cfg.home.nearby else ""
    return (
        f"Area: {cfg.home.label}{nearby}, within ~{cfg.search.max_distance_miles:.0f} miles.\n"
        + (f"{focus}\n" if focus else "") + "\n"
        f"{profile_text(cfg.profile)}\n\n"
        f"Already tracked domains (do not suggest these): {', '.join(sorted(known_domains)) or 'none'}\n"
        f"Blocked domains: {', '.join(cfg.discovery.blocked_domains)}"
    )


def pick_candidates(llm: LLM, cfg: Config, material: str, known_domains: set[str], focus: str | None = None) -> list[dict]:
    user = (
        discovery_brief(cfg, known_domains, focus)
        + f"\n\nFrom the research material below, choose up to {cfg.discovery.max_candidates_per_run} of the most promising "
          "event-listing pages. Only use URLs that appear in the material.\n\n<material>\n" + material + "\n</material>"
    )
    data = llm.json(system=DISCOVER_SYSTEM, user=user, schema=CANDIDATE_SCHEMA, task="discover")
    return data.get("candidates", [])
