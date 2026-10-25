"""PostgreSQL persistence: sources, events, score/extraction caches, geocode caches, run history.

Connections come from a psycopg pool. Each session runs with TimeZone set to the home timezone, so timestamps come
back as aware datetimes in local time, and with search_path set to the configured schema.
Fragments of SQL passed in by callers (e.g. the `where` of `upcoming_events`) use psycopg's %s placeholders.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Iterator
from urllib.parse import urlparse

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout
from rapidfuzz import fuzz

from eventscout.models import Destination, DestinationSource, Event, RawEvent, Source, Trip

if TYPE_CHECKING:
    from eventscout.config import DatabaseConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT NOT NULL DEFAULT 'seed',
    options JSONB NOT NULL DEFAULT '{}',
    notes TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_at TIMESTAMPTZ,
    last_ok_at TIMESTAMPTZ,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    last_event_count INTEGER NOT NULL DEFAULT 0,
    relevant_events INTEGER NOT NULL DEFAULT 0
);

-- One-time trip reports: events for a city you're visiting, kept apart from home events.
CREATE TABLE IF NOT EXISTS trips (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    timezone TEXT NOT NULL,
    city TEXT,
    region TEXT,
    region_code TEXT,
    country TEXT,
    country_code TEXT,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    radius_miles DOUBLE PRECISION NOT NULL DEFAULT 10,
    near TEXT,
    notes TEXT NOT NULL DEFAULT '',
    discover BOOLEAN NOT NULL DEFAULT true,
    status TEXT NOT NULL DEFAULT 'queued',
    sources JSONB NOT NULL DEFAULT '[]',
    stats JSONB NOT NULL DEFAULT '{}',
    log TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    built_at TIMESTAMPTZ
);

-- Saved per-city source lists, reused by every trip to that city (key: see models.Trip.dest_key).
CREATE TABLE IF NOT EXISTS destinations (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    builtins_off JSONB NOT NULL DEFAULT '[]',  -- built-in sources switched off for this city
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS destination_sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dest_key TEXT NOT NULL REFERENCES destinations(key) ON DELETE CASCADE,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    options JSONB NOT NULL DEFAULT '{}',
    enabled BOOLEAN NOT NULL DEFAULT true,
    origin TEXT NOT NULL DEFAULT 'manual',  -- manual | discovered
    notes TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_built_at TIMESTAMPTZ,
    last_event_count INTEGER,
    last_error TEXT,
    UNIQUE (dest_key, url)
);

CREATE TABLE IF NOT EXISTS events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trip_id BIGINT REFERENCES trips(id) ON DELETE CASCADE,  -- NULL for home events
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    start TIMESTAMPTZ NOT NULL,
    start_date DATE NOT NULL,
    "end" TIMESTAMPTZ,
    all_day BOOLEAN NOT NULL DEFAULT false,
    time_known BOOLEAN NOT NULL DEFAULT true,
    description TEXT NOT NULL DEFAULT '',
    venue_name TEXT,
    address TEXT,
    city TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    location_approx BOOLEAN NOT NULL DEFAULT false,
    distance_miles DOUBLE PRECISION,
    drive_minutes DOUBLE PRECISION,
    price_min DOUBLE PRECISION,
    price_max DOUBLE PRECISION,
    is_free BOOLEAN,
    cost_text TEXT,
    currency TEXT,
    url TEXT,
    image_url TEXT,
    categories JSONB NOT NULL DEFAULT '[]',
    online BOOLEAN NOT NULL DEFAULT false,
    source_ids JSONB NOT NULL DEFAULT '[]',
    source_names JSONB NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    filter_reason TEXT,
    score INTEGER,
    score_reason TEXT,
    score_detail JSONB,
    tags JSONB NOT NULL DEFAULT '[]',
    score_key TEXT,
    feedback SMALLINT NOT NULL DEFAULT 0,
    starred BOOLEAN NOT NULL DEFAULT false,
    enriched BOOLEAN NOT NULL DEFAULT false,
    geo_attempted BOOLEAN NOT NULL DEFAULT false,
    image_checked BOOLEAN NOT NULL DEFAULT false,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_start_date ON events (start_date);
CREATE INDEX IF NOT EXISTS idx_events_status_date ON events (status, start_date);

CREATE TABLE IF NOT EXISTS score_cache (
    key TEXT PRIMARY KEY,
    score INTEGER NOT NULL,
    reason TEXT,
    tags JSONB NOT NULL DEFAULT '[]',
    detail JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS extraction_cache (
    key TEXT PRIMARY KEY,
    events JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feedback_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    venue TEXT,
    value SMALLINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS geocache (
    query TEXT PRIMARY KEY,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS routecache (
    key TEXT PRIMARY KEY,
    minutes DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    stats JSONB NOT NULL DEFAULT '{}',
    log TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after a table's first release, applied to existing databases at startup.
MIGRATIONS = [
    'ALTER TABLE events ADD COLUMN IF NOT EXISTS image_checked BOOLEAN NOT NULL DEFAULT false',
    'ALTER TABLE events ADD COLUMN IF NOT EXISTS score_detail JSONB',
    'ALTER TABLE score_cache ADD COLUMN IF NOT EXISTS detail JSONB',
    'ALTER TABLE events ADD COLUMN IF NOT EXISTS trip_id BIGINT REFERENCES trips(id) ON DELETE CASCADE',
    'CREATE INDEX IF NOT EXISTS idx_events_trip_date ON events (trip_id, start_date)',
]

_JSON_COLUMNS = {"categories", "tags", "source_ids", "source_names", "options", "stats", "score_detail", "sources",
                 "builtins_off"}
_BOOL_COLUMNS = {"all_day", "time_known", "location_approx", "online", "starred", "enriched", "geo_attempted",
                 "image_checked", "is_free", "discover", "enabled"}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_STOPWORDS = {"the", "a", "an", "and", "at", "with", "of", "in", "on", "for", "live", "presents", "w", "feat", "ft"}

_open_databases: "weakref.WeakSet[Database]" = weakref.WeakSet()


def normalize_title(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return " ".join(w for w in words if w not in _STOPWORDS)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _adapt(col: str, value: Any) -> Any:
    """Python value -> what the column expects (JSONB wrapper, real booleans)."""
    if col in _JSON_COLUMNS and value is not None and not isinstance(value, Jsonb):
        return Jsonb(value)
    if col in _BOOL_COLUMNS and value is not None:
        return bool(value)
    return value


def _set_clause(fields: dict[str, Any]) -> tuple[str, list[Any]]:
    for col in fields:
        if not _IDENT.match(col):
            raise ValueError(f"bad column name {col!r}")
    return ", ".join(f'"{c}" = %s' for c in fields), [_adapt(c, v) for c, v in fields.items()]


def conninfo_for(cfg: "DatabaseConfig", tz: str) -> str:
    if not _IDENT.match(cfg.schema_name):
        raise ValueError(f"database.schema must be a plain identifier, got {cfg.schema_name!r}")
    options = f"-c search_path={cfg.schema_name} -c TimeZone={tz}"
    if cfg.url:
        return make_conninfo(cfg.url, options=options, application_name="eventscout", connect_timeout=10)
    return make_conninfo(
        host=cfg.host, port=cfg.port, dbname=cfg.name, user=cfg.user, password=cfg.password or None,
        options=options, application_name="eventscout", connect_timeout=10,
    )


def describe(cfg: "DatabaseConfig") -> str:
    """Where we connect, without the password."""
    if cfg.url:
        p = urlparse(cfg.url)
        return f"{p.hostname}:{p.port or 5432}{p.path}"
    return f"{cfg.user}@{cfg.host}:{cfg.port}/{cfg.name}"


class DatabaseUnavailable(RuntimeError):
    pass


class Database:
    def __init__(self, cfg: "DatabaseConfig", tz: str = "UTC"):
        self.cfg = cfg
        self.pool = ConnectionPool(
            conninfo_for(cfg, tz), min_size=1, max_size=cfg.pool_size, open=False,
            kwargs={"row_factory": dict_row}, name="eventscout",
            check=ConnectionPool.check_connection,  # replace connections killed by a server restart
        )
        try:
            self.pool.open(wait=True, timeout=cfg.connect_timeout)
        except PoolTimeout as exc:
            self.pool.close()
            raise DatabaseUnavailable(
                f"could not connect to PostgreSQL at {describe(cfg)} within {cfg.connect_timeout}s "
                f"(check database settings / POSTGRES_* variables and that the server is up)"
            ) from exc
        _open_databases.add(self)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn() as c:
            # Serialize schema setup across processes (e.g. `eventscout run` while `serve` is starting).
            c.execute("SELECT pg_advisory_xact_lock(hashtext('eventscout-schema'))")
            c.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.cfg.schema_name}"')
            c.execute(SCHEMA)
            for stmt in MIGRATIONS:
                c.execute(stmt)

    def close(self) -> None:
        self.pool.close()
        _open_databases.discard(self)

    @staticmethod
    def close_all() -> None:
        for db in list(_open_databases):
            db.close()

    @contextmanager
    def conn(self) -> Iterator[psycopg.Connection]:
        """A pooled connection; commits when the block exits normally, rolls back on error."""
        with self.pool.connection() as conn:
            yield conn

    def ping(self, timeout: float = 3) -> bool:
        """Quick liveness check for /healthz: never blocks for long when the server is down."""
        try:
            with self.pool.connection(timeout=timeout) as c:
                c.execute("SELECT 1")
            return True
        except (psycopg.Error, PoolTimeout):
            return False

    # ------------------------------------------------------------------ kv
    def get_kv(self, key: str) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = %s", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO kv (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )

    # ------------------------------------------------------------- sources
    def upsert_seed_source(self, name: str, url: str, kind: str, options: dict, enabled: bool) -> None:
        """Seeds from config are inserted once; later edits to name/kind/options are synced, status is not."""
        with self.conn() as c:
            row = c.execute("SELECT id, status, kind, options FROM sources WHERE url = %s", (url,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO sources (name, url, kind, status, origin, options) VALUES (%s, %s, %s, %s, 'seed', %s)",
                    (name, url, kind, "active" if enabled else "disabled", Jsonb(options)),
                )
                return
            status = row["status"]
            if not enabled:
                status = "disabled"
            elif status == "disabled":
                status = "active"
            old_opts = row["options"] or {}
            merged = {**options, **{k: v for k, v in old_opts.items() if k.startswith("_")}}
            # An 'auto' seed keeps whatever kind detection resolved it to.
            new_kind = row["kind"] if kind == "auto" and old_opts.get("_resolved") else kind
            c.execute(
                "UPDATE sources SET name = %s, options = %s, status = %s, kind = %s WHERE id = %s",
                (name, Jsonb(merged), status, new_kind, row["id"]),
            )

    def disable_removed_seeds(self, seed_urls: set[str]) -> list[str]:
        """Seeds deleted from (or re-pointed in) the config are disabled rather than left running."""
        with self.conn() as c:
            rows = c.execute(
                "UPDATE sources SET status = 'disabled', notes = 'removed from config' "
                "WHERE origin = 'seed' AND status != 'disabled' AND NOT (url = ANY(%s)) RETURNING name",
                (list(seed_urls),),
            ).fetchall()
        return [r["name"] for r in rows]

    def add_source(self, name: str, url: str, kind: str, status: str, origin: str, options: dict | None = None,
                   notes: str | None = None) -> int | None:
        """Returns the new id, or None if a source with this URL already exists."""
        with self.conn() as c:
            row = c.execute(
                "INSERT INTO sources (name, url, kind, status, origin, options, notes) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (url) DO NOTHING RETURNING id",
                (name, url, kind, status, origin, Jsonb(options or {}), notes),
            ).fetchone()
        return row["id"] if row else None

    def list_sources(self, statuses: Iterable[str] | None = None) -> list[Source]:
        with self.conn() as c:
            if statuses:
                rows = c.execute("SELECT * FROM sources WHERE status = ANY(%s) ORDER BY name", (list(statuses),)).fetchall()
            else:
                rows = c.execute("SELECT * FROM sources ORDER BY status, name").fetchall()
        return [Source.from_row(r) for r in rows]

    def get_source(self, source_id: int) -> Source | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()
        return Source.from_row(row) if row else None

    def known_domains(self) -> set[str]:
        with self.conn() as c:
            return {domain_of(r["url"]) for r in c.execute("SELECT url FROM sources").fetchall()}

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, values = _set_clause(fields)
        with self.conn() as c:
            c.execute(f"UPDATE sources SET {cols} WHERE id = %s", (*values, source_id))

    def record_source_result(self, source_id: int, count: int, error: str | None) -> None:
        with self.conn() as c:
            if error:
                c.execute(
                    "UPDATE sources SET last_run_at = now(), last_error = %s, consecutive_failures = consecutive_failures + 1, "
                    "runs = runs + 1, last_event_count = 0 WHERE id = %s",
                    (error[:500], source_id),
                )
            else:
                c.execute(
                    "UPDATE sources SET last_run_at = now(), last_ok_at = now(), last_error = NULL, consecutive_failures = 0, "
                    "runs = runs + 1, last_event_count = %s WHERE id = %s",
                    (count, source_id),
                )

    def delete_source(self, source_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM sources WHERE id = %s", (source_id,))

    # -------------------------------------------------------------- events
    def upsert_event(self, ev: RawEvent, source: Source, trip_id: int | None = None) -> int:
        """Insert or merge an event. Near-duplicates (same day, similar title, close start) are merged.
        Home events (trip_id None) and each trip's events are deduplicated separately."""
        norm = normalize_title(ev.title)
        start_date = ev.start.date()
        scope = f"trip{trip_id}:" if trip_id else ""
        with self.conn() as c:
            # Serialize merges for this day so two writers can't both decide "new" for the same event.
            c.execute("SELECT pg_advisory_xact_lock(hashtext('eventscout-day:' || %s))", (scope + start_date.isoformat(),))
            candidates = c.execute("SELECT * FROM events WHERE start_date = %s AND trip_id IS NOT DISTINCT FROM %s",
                                   (start_date, trip_id)).fetchall()
            match = None
            for row in candidates:
                if row["norm_title"] == norm or fuzz.token_set_ratio(row["norm_title"], norm) >= 90:
                    if row["venue_name"] and ev.venue_name and fuzz.token_set_ratio(
                        row["venue_name"].lower(), ev.venue_name.lower()
                    ) < 60:
                        continue  # same-named event at a different venue
                    close = abs((row["start"] - ev.start).total_seconds()) <= 90 * 60
                    if close or not row["time_known"] or not ev.time_known:
                        match = row
                        break
            if match is None:
                fp = hashlib.sha1(f"{scope}{norm}|{start_date.isoformat()}|{(ev.venue_name or '').lower()}".encode()).hexdigest()
                return c.execute(
                    """INSERT INTO events (trip_id, fingerprint, title, norm_title, start, start_date, "end", all_day, time_known,
                        description, venue_name, address, city, latitude, longitude, price_min, price_max, is_free,
                        cost_text, currency, url, image_url, categories, online, source_ids, source_names)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (fingerprint) DO UPDATE SET last_seen = now()
                       RETURNING id""",
                    (
                        trip_id, fp, ev.title, norm, ev.start, start_date, ev.end, ev.all_day, ev.time_known,
                        ev.description[:4000], ev.venue_name, ev.address, ev.city, ev.latitude, ev.longitude,
                        ev.price_min, ev.price_max, ev.is_free, ev.cost_text, ev.currency, ev.url, ev.image_url,
                        Jsonb(ev.categories), ev.online, Jsonb([source.id]), Jsonb([source.name]),
                    ),
                ).fetchone()["id"]

            # Merge: fill gaps, prefer known times and richer descriptions.
            updates: dict[str, Any] = {}
            if not match["time_known"] and ev.time_known:
                updates.update(start=ev.start, time_known=True, all_day=ev.all_day)
            fill = {
                "end": ev.end, "venue_name": ev.venue_name, "address": ev.address, "city": ev.city,
                "latitude": ev.latitude, "longitude": ev.longitude, "price_min": ev.price_min, "price_max": ev.price_max,
                "is_free": ev.is_free, "cost_text": ev.cost_text, "currency": ev.currency, "url": ev.url,
                "image_url": ev.image_url,
            }
            for key, val in fill.items():
                if val is not None and match[key] is None:
                    updates[key] = val
            if len(ev.description) > len(match["description"] or ""):
                updates["description"] = ev.description[:4000]
            updates["categories"] = list(dict.fromkeys(list(match["categories"]) + ev.categories))
            ids, names = list(match["source_ids"]), list(match["source_names"])
            if source.id not in ids:
                ids.append(source.id)
                names.append(source.name)
            updates["source_ids"], updates["source_names"] = ids, names
            cols, values = _set_clause(updates)
            c.execute(f"UPDATE events SET {cols}, last_seen = now() WHERE id = %s", (*values, match["id"]))
            return match["id"]

    def update_event(self, event_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, values = _set_clause(fields)
        with self.conn() as c:
            c.execute(f"UPDATE events SET {cols} WHERE id = %s", (*values, event_id))

    def get_event(self, event_id: int) -> Event | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM events WHERE id = %s", (event_id,)).fetchone()
        return Event.from_row(row) if row else None

    def upcoming_events(self, start: datetime, end: datetime, where: str = "", params: tuple = (),
                        trip_id: int | None = None) -> list[Event]:
        """Home events (or one trip's, with trip_id) whose start date falls in [start.date(), end.date()],
        optionally narrowed by a SQL `where` fragment that uses %s placeholders (write a literal % as %%)."""
        q = "SELECT * FROM events WHERE trip_id IS NOT DISTINCT FROM %s AND start_date >= %s AND start_date <= %s"
        if where:
            q += f" AND ({where})"
        q += " ORDER BY start"
        with self.conn() as c:
            rows = c.execute(q, (trip_id, start.date(), end.date(), *params)).fetchall()
        return [Event.from_row(r) for r in rows]

    def drop_generic_images(self, min_titles: int = 4) -> int:
        """An image shared by many different events is a site logo or placeholder, not an event photo."""
        with self.conn() as c:
            return c.execute(
                "UPDATE events SET image_url = NULL WHERE image_url IN ("
                "SELECT image_url FROM events WHERE image_url IS NOT NULL "
                "GROUP BY image_url HAVING COUNT(DISTINCT norm_title) >= %s)",
                (min_titles,),
            ).rowcount

    def prune_events(self, before: datetime) -> int:
        """Drop old events that the user never interacted with."""
        with self.conn() as c:
            return c.execute(
                "DELETE FROM events WHERE trip_id IS NULL AND start_date < %s AND feedback = 0 AND NOT starred",
                (before.date(),)
            ).rowcount

    # ------------------------------------------------------------ feedback
    def set_feedback(self, event_id: int, value: int) -> None:
        with self.conn() as c:
            row = c.execute("UPDATE events SET feedback = %s WHERE id = %s RETURNING title, venue_name",
                            (value, event_id)).fetchone()
            if row is None:
                return
            c.execute("DELETE FROM feedback_log WHERE event_id = %s", (event_id,))
            if value != 0:
                c.execute("INSERT INTO feedback_log (event_id, title, venue, value) VALUES (%s, %s, %s, %s)",
                          (event_id, row["title"], row["venue_name"], value))

    def set_starred(self, event_id: int, starred: bool) -> None:
        with self.conn() as c:
            row = c.execute("UPDATE events SET starred = %s WHERE id = %s RETURNING feedback",
                            (starred, event_id)).fetchone()
        if starred and row and row["feedback"] == 0:
            self.set_feedback(event_id, 1)

    def feedback_examples(self, limit: int = 40) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT title, venue, value FROM feedback_log ORDER BY id DESC LIMIT %s", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------- extraction cache
    def get_extraction(self, key: str) -> list[dict] | None:
        with self.conn() as c:
            row = c.execute("SELECT events FROM extraction_cache WHERE key = %s", (key,)).fetchone()
        return row["events"] if row else None

    def put_extraction(self, key: str, events: list[dict]) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO extraction_cache (key, events) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET events = EXCLUDED.events, created_at = now()",
                (key, Jsonb(events)),
            )

    def prune_extractions(self, older_than_days: int = 14) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM extraction_cache WHERE created_at < now() - make_interval(days => %s)",
                      (older_than_days,))

    def clear_scores(self) -> int:
        """Force re-scoring of all stored events on the next run."""
        with self.conn() as c:
            c.execute("DELETE FROM score_cache")
            return c.execute("UPDATE events SET score_key = NULL").rowcount

    # --------------------------------------------------------- score cache
    def get_cached_score(self, key: str) -> tuple[int, str, list[str], dict | None] | None:
        with self.conn() as c:
            row = c.execute("SELECT score, reason, tags, detail FROM score_cache WHERE key = %s", (key,)).fetchone()
        return (row["score"], row["reason"], list(row["tags"]), row["detail"]) if row else None

    def put_cached_score(self, key: str, score: int, reason: str, tags: list[str], detail: dict | None = None) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO score_cache (key, score, reason, tags, detail) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET score = EXCLUDED.score, reason = EXCLUDED.reason, "
                "tags = EXCLUDED.tags, detail = EXCLUDED.detail, created_at = now()",
                (key, score, reason, Jsonb(tags), Jsonb(detail) if detail is not None else None),
            )

    # ---------------------------------------------------------- geo caches
    def get_geocode(self, query: str) -> tuple[float | None, float | None] | None:
        with self.conn() as c:
            row = c.execute("SELECT latitude, longitude FROM geocache WHERE query = %s", (query,)).fetchone()
        return (row["latitude"], row["longitude"]) if row else None

    def put_geocode(self, query: str, lat: float | None, lon: float | None) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO geocache (query, latitude, longitude) VALUES (%s, %s, %s) ON CONFLICT (query) DO UPDATE "
                "SET latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude, created_at = now()",
                (query, lat, lon),
            )

    def get_route(self, key: str) -> tuple[float | None] | None:
        with self.conn() as c:
            row = c.execute("SELECT minutes FROM routecache WHERE key = %s", (key,)).fetchone()
        return (row["minutes"],) if row else None

    def put_route(self, key: str, minutes: float | None) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO routecache (key, minutes) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET minutes = EXCLUDED.minutes, created_at = now()",
                (key, minutes),
            )

    # --------------------------------------------------------------- trips
    def create_trip(self, **fields: Any) -> int:
        cols, values = _set_clause(fields)
        names = ", ".join(f'"{k}"' for k in fields)
        with self.conn() as c:
            return c.execute(f"INSERT INTO trips ({names}) VALUES ({', '.join(['%s'] * len(fields))}) RETURNING id",
                             values).fetchone()["id"]

    def get_trip(self, trip_id: int) -> Trip | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM trips WHERE id = %s", (trip_id,)).fetchone()
        return Trip.model_validate(dict(row)) if row else None

    def list_trips(self) -> list[Trip]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT t.*, (SELECT count(*) FROM events e WHERE e.trip_id = t.id AND e.status = 'ok') AS event_count "
                "FROM trips t ORDER BY t.start_date DESC, t.id DESC"
            ).fetchall()
        return [Trip.model_validate(dict(r)) for r in rows]

    def update_trip(self, trip_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, values = _set_clause(fields)
        with self.conn() as c:
            c.execute(f"UPDATE trips SET {cols} WHERE id = %s", (*values, trip_id))

    def delete_trip(self, trip_id: int) -> None:
        with self.conn() as c:  # its events go too (ON DELETE CASCADE)
            c.execute("DELETE FROM trips WHERE id = %s", (trip_id,))

    # -------------------------------------------------------- destinations
    def ensure_destination(self, key: str, label: str) -> Destination:
        with self.conn() as c:
            c.execute("INSERT INTO destinations (key, label) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, label))
            row = c.execute("SELECT * FROM destinations WHERE key = %s", (key,)).fetchone()
        return Destination.model_validate(dict(row))

    def get_destination(self, key: str) -> Destination | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM destinations WHERE key = %s", (key,)).fetchone()
        return Destination.model_validate(dict(row)) if row else None

    def list_destinations(self) -> list[Destination]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT d.*, (SELECT count(*) FROM destination_sources s WHERE s.dest_key = d.key) AS source_count "
                "FROM destinations d ORDER BY d.label"
            ).fetchall()
        return [Destination.model_validate(dict(r)) for r in rows]

    def set_builtin_enabled(self, key: str, builtin: str, enabled: bool) -> None:
        with self.conn() as c:
            row = c.execute("SELECT builtins_off FROM destinations WHERE key = %s FOR UPDATE", (key,)).fetchone()
            off = [b for b in (row["builtins_off"] if row else []) if b != builtin]
            if not enabled:
                off.append(builtin)
            c.execute("UPDATE destinations SET builtins_off = %s WHERE key = %s", (Jsonb(sorted(off)), key))

    def list_destination_sources(self, key: str, enabled_only: bool = False) -> list[DestinationSource]:
        q = "SELECT * FROM destination_sources WHERE dest_key = %s" + (" AND enabled" if enabled_only else "")
        with self.conn() as c:
            rows = c.execute(q + " ORDER BY name", (key,)).fetchall()
        return [DestinationSource.model_validate(dict(r)) for r in rows]

    def add_destination_source(self, key: str, name: str, url: str, kind: str, options: dict | None = None,
                               origin: str = "manual", notes: str | None = None) -> int | None:
        """Returns the new id, or None if this city already has a source with that URL."""
        with self.conn() as c:
            row = c.execute(
                "INSERT INTO destination_sources (dest_key, name, url, kind, options, origin, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (dest_key, url) DO NOTHING RETURNING id",
                (key, name, url, kind, Jsonb(options or {}), origin, notes),
            ).fetchone()
        return row["id"] if row else None

    def get_destination_source(self, source_id: int) -> DestinationSource | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM destination_sources WHERE id = %s", (source_id,)).fetchone()
        return DestinationSource.model_validate(dict(row)) if row else None

    def update_destination_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols, values = _set_clause(fields)
        with self.conn() as c:
            c.execute(f"UPDATE destination_sources SET {cols} WHERE id = %s", (*values, source_id))

    def delete_destination_source(self, source_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM destination_sources WHERE id = %s", (source_id,))

    # ---------------------------------------------------------------- runs
    def start_run(self, kind: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO runs (kind) VALUES (%s) RETURNING id", (kind,)).fetchone()["id"]

    def finish_run(self, run_id: int, status: str, stats: dict, log: str) -> None:
        stats = json.loads(json.dumps(stats, default=str))  # make datetimes etc. JSON-safe
        with self.conn() as c:
            c.execute(
                "UPDATE runs SET finished_at = now(), status = %s, stats = %s, log = %s WHERE id = %s",
                (status, Jsonb(stats), log[-100_000:], run_id),
            )

    def recent_runs(self, limit: int = 30) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT %s", (limit,)).fetchall()]

    def mark_stale_runs(self) -> None:
        """Runs (and trip builds) left 'running' by a crash/restart are marked as aborted."""
        with self.conn() as c:
            c.execute("UPDATE runs SET status = 'aborted', finished_at = now() WHERE status = 'running'")
            c.execute("UPDATE trips SET status = 'error', log = log || E'\\nInterrupted by a restart; rebuild to finish.' "
                      "WHERE status IN ('queued', 'running')")

