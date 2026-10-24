"""Cheap, deterministic filters applied before any LLM scoring."""

from __future__ import annotations

import re
from functools import lru_cache

from eventscout.config import Config
from eventscout.models import Event


@lru_cache(maxsize=32)
def _keyword_re(words: tuple[str, ...]) -> re.Pattern | None:
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", re.I)


def hard_filter(ev: Event, cfg: Config) -> str | None:
    """Return a reason string if the event should be excluded, else None."""
    s = cfg.search
    if ev.online and not s.include_online:
        return "online-only"
    kw = _keyword_re(tuple(k.lower() for k in cfg.profile.exclude_keywords))
    if kw:
        m = kw.search(f"{ev.title} {ev.description} {' '.join(ev.categories)}")
        if m:
            return f"excluded keyword: {m.group(0).lower()}"
    if s.max_price is not None and ev.price_min is not None and not ev.is_free and ev.price_min > s.max_price:
        return f"price ${ev.price_min:.0f} > ${s.max_price:.0f}"
    if ev.distance_miles is not None and ev.distance_miles > s.max_distance_miles:
        return f"{ev.distance_miles:.0f} mi away"
    if s.max_drive_minutes and ev.drive_minutes is not None and ev.drive_minutes > s.max_drive_minutes:
        return f"{ev.drive_minutes:.0f} min drive"
    return None
