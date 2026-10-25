"""Core data models."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class RawEvent(BaseModel):
    """An event as produced by a source adapter, before dedupe/filtering/scoring."""

    title: str
    start: datetime  # timezone-aware
    end: datetime | None = None
    all_day: bool = False
    time_known: bool = True  # False when the source only gave a date
    description: str = ""
    venue_name: str | None = None
    address: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    price_min: float | None = None
    price_max: float | None = None
    is_free: bool | None = None
    cost_text: str | None = None
    currency: str | None = None
    url: str | None = None
    image_url: str | None = None
    categories: list[str] = Field(default_factory=list)
    online: bool = False

    @field_validator("image_url", mode="before")
    @classmethod
    def _safe_image(cls, v: object) -> str | None:
        from eventscout.images import clean_image_url

        return clean_image_url(v)


class Event(RawEvent):
    """A stored event."""

    id: int
    trip_id: int | None = None  # None for home events
    fingerprint: str
    source_ids: list[int] = Field(default_factory=list)
    source_names: list[str] = Field(default_factory=list)
    distance_miles: float | None = None
    drive_minutes: float | None = None
    location_approx: bool = False
    status: str = "new"  # new | ok | filtered
    filter_reason: str | None = None
    score: int | None = None
    score_reason: str | None = None
    score_detail: dict | None = None  # {"matched": [...], "against": [...], "factors": [...]} (see tasks.Score)
    tags: list[str] = Field(default_factory=list)
    score_key: str | None = None
    feedback: int = 0  # -1 not interested, 0 none, 1 liked
    starred: bool = False
    enriched: bool = False
    first_seen: datetime
    last_seen: datetime

    @property
    def cost_display(self) -> str:
        if self.is_free:
            return "Free"
        if self.price_min is not None:
            sym = "$" if (self.currency or "USD") == "USD" else f"{self.currency} "
            lo = f"{sym}{self.price_min:,.0f}"
            if self.price_max is not None and self.price_max > self.price_min:
                return f"{lo}–{sym}{self.price_max:,.0f}"
            return lo
        return self.cost_text or "Price n/a"

    @property
    def location_display(self) -> str:
        parts = [p for p in (self.venue_name, self.city) if p]
        if not parts and self.address:
            parts = [self.address]
        return ", ".join(dict.fromkeys(parts)) or ("Online" if self.online else "Location TBD")

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        d = dict(row)
        for key in ("categories", "tags", "source_ids", "source_names"):
            if isinstance(d.get(key), str):
                d[key] = json.loads(d[key])
            d[key] = d.get(key) or []
        return cls.model_validate(d)


class Source(BaseModel):
    id: int
    name: str
    url: str
    kind: str
    status: str  # active | trial | disabled | retired
    origin: str  # seed | discovered | manual
    options: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None
    added_at: datetime
    last_run_at: datetime | None = None
    last_ok_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    runs: int = 0
    last_event_count: int = 0
    relevant_events: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "Source":
        d = dict(row)
        if isinstance(d.get("options"), str):
            d["options"] = json.loads(d["options"])
        d["options"] = d.get("options") or {}
        return cls.model_validate(d)


def dest_key_for(city: str, region_code: str | None, country_code: str | None) -> str:
    """A city's key for its saved trip sources, e.g. "chicago|us-il|us"."""
    slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    return f"{slug}|{(region_code or '').lower()}|{(country_code or '').lower()}"


class Trip(BaseModel):
    """A one-time report for a city you're visiting."""

    id: int
    name: str
    query: str
    latitude: float
    longitude: float
    timezone: str
    city: str | None = None
    region: str | None = None
    region_code: str | None = None  # e.g. "US-IL"
    country: str | None = None
    country_code: str | None = None  # e.g. "us"
    start_date: date
    end_date: date
    radius_miles: float = 10
    near: str | None = None
    notes: str = ""
    discover: bool = True
    status: str = "queued"  # queued | running | done | error
    sources: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    log: str = ""
    created_at: datetime
    built_at: datetime | None = None
    event_count: int | None = None

    @property
    def dest_key(self) -> str:
        """Identity of the destination, shared by every trip to the same city: saved sources hang off this."""
        return dest_key_for(self.city or self.name, self.region_code, self.country_code)

    @property
    def nights(self) -> int:
        return (self.end_date - self.start_date).days

    @property
    def dates_display(self) -> str:
        s, e = self.start_date, self.end_date
        if s == e:
            return s.strftime("%a %b %-d, %Y")
        if s.year == e.year and s.month == e.month:
            return f"{s:%b %-d}–{e:%-d}, {e:%Y}"
        return f"{s:%b %-d} – {e:%b %-d}, {e:%Y}"


class Destination(BaseModel):
    key: str
    label: str
    builtins_off: list[str] = Field(default_factory=list)
    place: dict[str, Any] | None = None  # the geocoded city (trips.Place fields), so it can be curated without a trip
    created_at: datetime
    source_count: int | None = None


class DestinationSource(BaseModel):
    """A source saved for one city, used by every trip there."""

    id: int
    dest_key: str
    name: str
    url: str
    kind: str
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    origin: str = "manual"  # manual | discovered
    notes: str | None = None
    added_at: datetime
    last_built_at: datetime | None = None
    last_event_count: int | None = None
    last_error: str | None = None
