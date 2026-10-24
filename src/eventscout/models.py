"""Core data models."""

from __future__ import annotations

import json
from datetime import datetime
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
            d[key] = json.loads(d.get(key) or "[]")
        for key in ("all_day", "time_known", "online", "starred", "enriched", "location_approx"):
            d[key] = bool(d.get(key))
        if d.get("is_free") is not None:
            d["is_free"] = bool(d["is_free"])
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
        d["options"] = json.loads(d.get("options") or "{}")
        return cls.model_validate(d)
