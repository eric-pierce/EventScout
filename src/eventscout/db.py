"""SQLite persistence: sources, events, score cache, geocode cache, run history."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

from rapidfuzz import fuzz

from eventscout.models import Event, RawEvent, Source

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    origin TEXT NOT NULL DEFAULT 'seed',
    options TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    added_at TEXT NOT NULL,
    last_run_at TEXT,
    last_ok_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    runs INTEGER NOT NULL DEFAULT 0,
    last_event_count INTEGER NOT NULL DEFAULT 0,
    relevant_events INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    start TEXT NOT NULL,
    start_date TEXT NOT NULL,
    "end" TEXT,
    all_day INTEGER NOT NULL DEFAULT 0,
    time_known INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    venue_name TEXT,
    address TEXT,
    city TEXT,
    latitude REAL,
    longitude REAL,
    location_approx INTEGER NOT NULL DEFAULT 0,
    distance_miles REAL,
    drive_minutes REAL,
    price_min REAL,
    price_max REAL,
    is_free INTEGER,
    cost_text TEXT,
    currency TEXT,
    url TEXT,
    image_url TEXT,
    categories TEXT NOT NULL DEFAULT '[]',
    online INTEGER NOT NULL DEFAULT 0,
    source_ids TEXT NOT NULL DEFAULT '[]',
    source_names TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'new',
    filter_reason TEXT,
    score INTEGER,
    score_reason TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    score_key TEXT,
    feedback INTEGER NOT NULL DEFAULT 0,
    starred INTEGER NOT NULL DEFAULT 0,
    enriched INTEGER NOT NULL DEFAULT 0,
    geo_attempted INTEGER NOT NULL DEFAULT 0,
    image_checked INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start_date ON events(start_date);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS score_cache (
    key TEXT PRIMARY KEY,
    score INTEGER NOT NULL,
    reason TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_log (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    venue TEXT,
    value INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocache (
    query TEXT PRIMARY KEY,
    latitude REAL,
    longitude REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routecache (
    key TEXT PRIMARY KEY,
    minutes REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    stats TEXT NOT NULL DEFAULT '{}',
    log TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_STOPWORDS = {"the", "a", "an", "and", "at", "with", "of", "in", "on", "for", "live", "presents", "w", "feat", "ft"}


def normalize_title(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return " ".join(w for w in words if w not in _STOPWORDS)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    # Columns added after the first release; ALTERed onto older databases.
    _ADDED_COLUMNS = {"events": {"image_checked": "INTEGER NOT NULL DEFAULT 0"}}

    def _migrate(self, c: sqlite3.Connection) -> None:
        for table, cols in self._ADDED_COLUMNS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        with self._write_lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------ kv
    def get_kv(self, key: str) -> str | None:
        with self.conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ------------------------------------------------------------- sources
    def upsert_seed_source(self, name: str, url: str, kind: str, options: dict, enabled: bool) -> None:
        """Seeds from config are inserted once; later edits to name/kind/options are synced, status is not."""
        with self.conn() as c:
            row = c.execute("SELECT id, status, kind, options FROM sources WHERE url=?", (url,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO sources(name, url, kind, status, origin, options, added_at) VALUES(?,?,?,?,?,?,?)",
                    (name, url, kind, "active" if enabled else "disabled", "seed", json.dumps(options), _utcnow()),
                )
            else:
                status = row["status"]
                if not enabled:
                    status = "disabled"
                elif status == "disabled":
                    status = "active"
                old_opts = json.loads(row["options"] or "{}")
                merged = {**options, **{k: v for k, v in old_opts.items() if k.startswith("_")}}
                # An 'auto' seed keeps whatever kind detection resolved it to.
                new_kind = row["kind"] if kind == "auto" and old_opts.get("_resolved") else kind
                c.execute(
                    "UPDATE sources SET name=?, options=?, status=?, kind=? WHERE id=?",
                    (name, json.dumps(merged), status, new_kind, row["id"]),
                )

    def disable_removed_seeds(self, seed_urls: set[str]) -> list[str]:
        """Seeds deleted from (or re-pointed in) the config are disabled rather than left running."""
        with self.conn() as c:
            rows = c.execute("SELECT id, name, url FROM sources WHERE origin='seed' AND status != 'disabled'").fetchall()
            gone = [r for r in rows if r["url"] not in seed_urls]
            for r in gone:
                c.execute("UPDATE sources SET status='disabled', notes='removed from config' WHERE id=?", (r["id"],))
        return [r["name"] for r in gone]

    def add_source(self, name: str, url: str, kind: str, status: str, origin: str, options: dict | None = None, notes: str | None = None) -> int | None:
        with self.conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO sources(name, url, kind, status, origin, options, notes, added_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, url, kind, status, origin, json.dumps(options or {}), notes, _utcnow()),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def list_sources(self, statuses: Iterable[str] | None = None) -> list[Source]:
        with self.conn() as c:
            if statuses:
                statuses = list(statuses)
                q = f"SELECT * FROM sources WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY name"
                rows = c.execute(q, statuses).fetchall()
            else:
                rows = c.execute("SELECT * FROM sources ORDER BY status, name").fetchall()
        return [Source.from_row(r) for r in rows]

    def get_source(self, source_id: int) -> Source | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return Source.from_row(row) if row else None

    def known_domains(self) -> set[str]:
        with self.conn() as c:
            return {domain_of(r["url"]) for r in c.execute("SELECT url FROM sources")}

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        if "options" in fields and isinstance(fields["options"], dict):
            fields["options"] = json.dumps(fields["options"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), source_id))

    def record_source_result(self, source_id: int, count: int, error: str | None) -> None:
        now = _utcnow()
        with self.conn() as c:
            if error:
                c.execute(
                    "UPDATE sources SET last_run_at=?, last_error=?, consecutive_failures=consecutive_failures+1, runs=runs+1, last_event_count=0 WHERE id=?",
                    (now, error[:500], source_id),
                )
            else:
                c.execute(
                    "UPDATE sources SET last_run_at=?, last_ok_at=?, last_error=NULL, consecutive_failures=0, runs=runs+1, last_event_count=? WHERE id=?",
                    (now, now, count, source_id),
                )

    def delete_source(self, source_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM sources WHERE id=?", (source_id,))

    # -------------------------------------------------------------- events
    def upsert_event(self, ev: RawEvent, source: Source) -> int:
        """Insert or merge an event. Near-duplicates (same day, similar title, close start) are merged."""
        norm = normalize_title(ev.title)
        start_date = ev.start.date().isoformat()
        now = _utcnow()
        with self.conn() as c:
            candidates = c.execute(
                "SELECT * FROM events WHERE start_date=?", (start_date,)
            ).fetchall()
            match = None
            for row in candidates:
                if row["norm_title"] == norm or fuzz.token_set_ratio(row["norm_title"], norm) >= 90:
                    if row["venue_name"] and ev.venue_name and fuzz.token_set_ratio(
                        row["venue_name"].lower(), ev.venue_name.lower()
                    ) < 60:
                        continue  # same-named event at a different venue
                    existing_start = datetime.fromisoformat(row["start"])
                    close = abs((existing_start - ev.start).total_seconds()) <= 90 * 60
                    if close or not row["time_known"] or not ev.time_known:
                        match = row
                        break
            if match is None:
                fp = hashlib.sha1(f"{norm}|{start_date}|{(ev.venue_name or '').lower()}".encode()).hexdigest()
                cur = c.execute(
                    """INSERT INTO events(fingerprint, title, norm_title, start, start_date, "end", all_day, time_known,
                        description, venue_name, address, city, latitude, longitude, price_min, price_max, is_free,
                        cost_text, currency, url, image_url, categories, online, source_ids, source_names, first_seen, last_seen)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(fingerprint) DO UPDATE SET last_seen=excluded.last_seen""",
                    (
                        fp, ev.title, norm, ev.start.isoformat(), start_date, ev.end.isoformat() if ev.end else None,
                        int(ev.all_day), int(ev.time_known), ev.description[:4000], ev.venue_name, ev.address, ev.city,
                        ev.latitude, ev.longitude, ev.price_min, ev.price_max,
                        None if ev.is_free is None else int(ev.is_free), ev.cost_text, ev.currency, ev.url, ev.image_url,
                        json.dumps(ev.categories), int(ev.online), json.dumps([source.id]), json.dumps([source.name]), now, now,
                    ),
                )
                if cur.lastrowid:
                    return cur.lastrowid
                return c.execute("SELECT id FROM events WHERE fingerprint=?", (fp,)).fetchone()["id"]

            # Merge: fill gaps, prefer known times and richer descriptions.
            updates: dict[str, Any] = {"last_seen": now}
            if not match["time_known"] and ev.time_known:
                updates.update(start=ev.start.isoformat(), time_known=1, all_day=int(ev.all_day))
            fill = {
                "end": ev.end.isoformat() if ev.end else None,
                "venue_name": ev.venue_name, "address": ev.address, "city": ev.city,
                "latitude": ev.latitude, "longitude": ev.longitude,
                "price_min": ev.price_min, "price_max": ev.price_max,
                "is_free": None if ev.is_free is None else int(ev.is_free),
                "cost_text": ev.cost_text, "currency": ev.currency, "url": ev.url, "image_url": ev.image_url,
            }
            for key, val in fill.items():
                if val is not None and match[key] is None:
                    updates[key] = val
            if len(ev.description) > len(match["description"] or ""):
                updates["description"] = ev.description[:4000]
            cats = list(dict.fromkeys(json.loads(match["categories"]) + ev.categories))
            updates["categories"] = json.dumps(cats)
            ids = json.loads(match["source_ids"])
            names = json.loads(match["source_names"])
            if source.id not in ids:
                ids.append(source.id)
                names.append(source.name)
            updates["source_ids"] = json.dumps(ids)
            updates["source_names"] = json.dumps(names)
            cols = ", ".join(f'"{k}"=?' for k in updates)
            c.execute(f"UPDATE events SET {cols} WHERE id=?", (*updates.values(), match["id"]))
            return match["id"]

    def update_event(self, event_id: int, **fields: Any) -> None:
        if not fields:
            return
        for key in ("categories", "tags", "source_ids", "source_names"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = json.dumps(fields[key])
        for key, val in list(fields.items()):
            if isinstance(val, datetime):
                fields[key] = val.isoformat()
            elif isinstance(val, bool):
                fields[key] = int(val)
        cols = ", ".join(f'"{k}"=?' for k in fields)
        with self.conn() as c:
            c.execute(f"UPDATE events SET {cols} WHERE id=?", (*fields.values(), event_id))

    def get_event(self, event_id: int) -> Event | None:
        with self.conn() as c:
            row = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return Event.from_row(row) if row else None

    def upcoming_events(self, start: datetime, end: datetime, where: str = "", params: tuple = ()) -> list[Event]:
        q = "SELECT * FROM events WHERE start_date >= ? AND start_date <= ?"
        if where:
            q += f" AND ({where})"
        q += " ORDER BY start"
        with self.conn() as c:
            rows = c.execute(q, (start.date().isoformat(), end.date().isoformat(), *params)).fetchall()
        return [Event.from_row(r) for r in rows]

    def drop_generic_images(self, min_titles: int = 4) -> int:
        """An image shared by many different events is a site logo or placeholder, not an event photo."""
        with self.conn() as c:
            return c.execute(
                "UPDATE events SET image_url = NULL WHERE image_url IN ("
                "SELECT image_url FROM events WHERE image_url IS NOT NULL "
                "GROUP BY image_url HAVING COUNT(DISTINCT norm_title) >= ?)",
                (min_titles,),
            ).rowcount

    def prune_events(self, before: datetime) -> int:
        """Drop old events that the user never interacted with."""
        with self.conn() as c:
            cur = c.execute(
                "DELETE FROM events WHERE start_date < ? AND feedback = 0 AND starred = 0",
                (before.date().isoformat(),),
            )
            return cur.rowcount

    # ------------------------------------------------------------ feedback
    def set_feedback(self, event_id: int, value: int) -> None:
        ev = self.get_event(event_id)
        if not ev:
            return
        with self.conn() as c:
            c.execute("UPDATE events SET feedback=? WHERE id=?", (value, event_id))
            if value != 0:
                c.execute(
                    "INSERT INTO feedback_log(event_id, title, venue, value, created_at) VALUES(?,?,?,?,?)",
                    (event_id, ev.title, ev.venue_name, value, _utcnow()),
                )
            else:
                c.execute("DELETE FROM feedback_log WHERE event_id=?", (event_id,))

    def set_starred(self, event_id: int, starred: bool) -> None:
        with self.conn() as c:
            c.execute("UPDATE events SET starred=? WHERE id=?", (int(starred), event_id))
        if starred:
            ev = self.get_event(event_id)
            if ev and ev.feedback == 0:
                self.set_feedback(event_id, 1)

    def feedback_examples(self, limit: int = 40) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT title, venue, value FROM feedback_log WHERE id IN (SELECT MAX(id) FROM feedback_log GROUP BY event_id) ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------- extraction cache
    def get_extraction(self, key: str) -> list[dict] | None:
        raw = self.get_kv(f"extract:{key}")
        return json.loads(raw)["events"] if raw else None

    def put_extraction(self, key: str, events: list[dict]) -> None:
        self.set_kv(f"extract:{key}", json.dumps({"t": _utcnow(), "events": events}, default=str))

    def prune_extractions(self, older_than_days: int = 14) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        with self.conn() as c:
            c.execute("DELETE FROM kv WHERE key LIKE 'extract:%' AND json_extract(value, '$.t') < ?", (cutoff,))

    def clear_scores(self) -> int:
        """Force re-scoring of all stored events on the next run."""
        with self.conn() as c:
            c.execute("DELETE FROM score_cache")
            return c.execute("UPDATE events SET score_key = NULL").rowcount

    # --------------------------------------------------------- score cache
    def get_cached_score(self, key: str) -> tuple[int, str, list[str]] | None:
        with self.conn() as c:
            row = c.execute("SELECT score, reason, tags FROM score_cache WHERE key=?", (key,)).fetchone()
        return (row["score"], row["reason"], json.loads(row["tags"])) if row else None

    def put_cached_score(self, key: str, score: int, reason: str, tags: list[str]) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO score_cache(key, score, reason, tags, created_at) VALUES(?,?,?,?,?)",
                (key, score, reason, json.dumps(tags), _utcnow()),
            )

    # ---------------------------------------------------------- geo caches
    def get_geocode(self, query: str) -> tuple[float | None, float | None] | None:
        with self.conn() as c:
            row = c.execute("SELECT latitude, longitude FROM geocache WHERE query=?", (query,)).fetchone()
        return (row["latitude"], row["longitude"]) if row else None

    def put_geocode(self, query: str, lat: float | None, lon: float | None) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO geocache VALUES(?,?,?,?)", (query, lat, lon, _utcnow()))

    def get_route(self, key: str) -> tuple[float | None] | None:
        with self.conn() as c:
            row = c.execute("SELECT minutes FROM routecache WHERE key=?", (key,)).fetchone()
        return (row["minutes"],) if row else None

    def put_route(self, key: str, minutes: float | None) -> None:
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO routecache VALUES(?,?,?)", (key, minutes, _utcnow()))

    # ---------------------------------------------------------------- runs
    def start_run(self, kind: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO runs(kind, started_at) VALUES(?, ?)", (kind, _utcnow())).lastrowid

    def finish_run(self, run_id: int, status: str, stats: dict, log: str) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, stats=?, log=? WHERE id=?",
                (_utcnow(), status, json.dumps(stats, default=str), log[-100_000:], run_id),
            )

    def recent_runs(self, limit: int = 30) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d["stats"] or "{}")
            out.append(d)
        return out

    def mark_stale_runs(self) -> None:
        """Runs left 'running' by a crash/restart are marked as aborted."""
        with self.conn() as c:
            c.execute("UPDATE runs SET status='aborted', finished_at=? WHERE status='running'", (_utcnow(),))


def horizon(now: datetime, days: int) -> tuple[datetime, datetime]:
    return now, now + timedelta(days=days)
