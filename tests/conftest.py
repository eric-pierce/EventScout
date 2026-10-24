from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from eventscout.config import Config, Interest, Profile, SourceSeed
from eventscout.llm.base import LLM
from eventscout.models import RawEvent, Source
from eventscout.sources import ADAPTERS, SourceAdapter
from eventscout.sources.base import Context

TZ = ZoneInfo("America/New_York")


class FakeLLM(LLM):
    """Deterministic stand-in: scores by keyword, extracts nothing unless told to."""

    name = "fake"

    def __init__(self, extract_events: list[dict] | None = None):
        super().__init__()
        self.extract_events = extract_events or []
        self.calls: list[str] = []

    def json(self, *, system, user, schema, task):
        self.calls.append(task)
        self.usage.add(inp=100, out=10)
        if task == "score":
            import json as _json

            items = _json.loads(user.split("Rate each event:\n", 1)[1])
            results = []
            for it in items:
                text = it["title"].lower()
                score = 90 if "jazz" in text else 70 if "comedy" in text else 20
                results.append({"id": it["id"], "score": score, "reason": "test", "tags": ["jazz"] if score == 90 else []})
            return {"results": results}
        if task == "extract":
            return {"events": self.extract_events}
        return {"candidates": []}


class FakeAdapter(SourceAdapter):
    kind = "fake"
    events: list[RawEvent] = []

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        return list(self.events)


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config(
        data_dir=str(tmp_path),
        profile=Profile(
            interests=[Interest(topic="jazz", weight=5), Interest(topic="comedy", weight=3)],
            exclude_keywords=["toddler"],
            min_score=60,
        ),
        # model_construct: "fake" is a test-only kind outside the config schema.
        sources=[SourceSeed.model_construct(name="Fake", url="https://fake.example/events", kind="fake", options={}, enabled=True)],
    )
    c.geocoding.nominatim_url = None
    c.geocoding.osrm_url = None
    c.report.output_dir = str(tmp_path / "output")
    c.report.enrich_per_run = 0
    return c


@pytest.fixture
def fake_adapter(monkeypatch):
    adapter = FakeAdapter()
    adapter.events = []
    monkeypatch.setitem(ADAPTERS, "fake", adapter)
    return adapter


@pytest.fixture
def ctx(cfg) -> Context:
    start = datetime(2026, 10, 1, 9, 0, tzinfo=TZ)
    return Context(cfg=cfg, fetcher=None, llm_factory=lambda: FakeLLM(), window_start=start,
                   window_end=start + timedelta(days=21))


def at(days: int, hour: int = 20) -> datetime:
    base = datetime.now(TZ).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base + timedelta(days=days)
