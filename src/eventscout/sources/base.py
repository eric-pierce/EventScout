"""Source adapter interface."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from eventscout.config import Config
from eventscout.http import Fetcher
from eventscout.llm import LLM
from eventscout.models import RawEvent, Source


@dataclass
class Context:
    cfg: Config
    fetcher: Fetcher
    llm_factory: callable  # () -> LLM, created lazily so structured-only runs never need an LLM
    window_start: datetime
    window_end: datetime
    log: logging.Logger = field(default_factory=lambda: logging.getLogger("eventscout.sources"))
    db: object | None = None  # eventscout.db.Database, used to cache LLM page extractions

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.cfg.home.timezone)

    @property
    def llm(self) -> LLM:
        return self.llm_factory()

    def in_window(self, ev: RawEvent) -> bool:
        start_day = ev.start.date()
        if self.window_start.date() <= start_day <= self.window_end.date():
            return True
        # Multi-day events already under way (festivals, weekend fairs) are kept if they began recently;
        # months-long exhibitions and "ongoing" listings are dropped as noise.
        if ev.end and ev.end >= self.window_start and start_day < self.window_start.date():
            return (self.window_start.date() - start_day).days <= 3
        return False


class SourceAdapter(ABC):
    kind: str

    @abstractmethod
    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        ...
