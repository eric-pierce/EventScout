from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
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
        self.systems: list[str] = []

    def json(self, *, system, user, schema, task):
        self.calls.append(task)
        self.systems.append(system)
        self.usage.add(inp=100, out=10)
        if task == "score":
            import json as _json

            items = _json.loads(user.split("Rate each event:\n", 1)[1])
            results = []
            for it in items:
                text = it["title"].lower()
                score = 90 if "jazz" in text else 70 if "comedy" in text else 20
                results.append({
                    "id": it["id"], "score": score, "reason": "test", "tags": ["jazz"] if score == 90 else [],
                    "matched": ["Jazz"] if score == 90 else ["comedy"] if score == 70 else [],
                    "against": [] if score > 20 else ["not one of your interests"],
                    "factors": ["Saturday evening"],
                })
            return {"results": results}
        if task == "extract":
            return {"events": self.extract_events}
        return {"candidates": []}


class FakeAdapter(SourceAdapter):
    kind = "fake"
    events: list[RawEvent] = []

    def fetch(self, source: Source, ctx: Context) -> list[RawEvent]:
        return list(self.events)


# Tests run against a real PostgreSQL; each test gets its own schema, dropped afterwards.
# Locally: docker run -d --name eventscout-pg-dev -e POSTGRES_USER=eventscout -e POSTGRES_PASSWORD=devpass \
#   -e POSTGRES_DB=eventscout -p 127.0.0.1:55432:5432 postgres:18-alpine
TEST_DATABASE_URL = os.environ.get(
    "EVENTSCOUT_TEST_DATABASE_URL", "postgresql://eventscout:devpass@127.0.0.1:55432/eventscout"
)


@pytest.fixture
def pg_schema():
    name = f"test_{uuid.uuid4().hex[:10]}"
    yield name
    from eventscout.db import Database

    Database.close_all()
    try:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True, connect_timeout=5) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    except psycopg.OperationalError:
        pass  # tests that never touched the database still pass without a server


@pytest.fixture
def db_config(pg_schema):
    from eventscout.config import DatabaseConfig

    return DatabaseConfig(url=TEST_DATABASE_URL, schema=pg_schema, connect_timeout=5, pool_size=4)


@pytest.fixture
def cfg(tmp_path, db_config) -> Config:
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
    c.database = db_config
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
