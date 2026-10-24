"""Geocoding (Nominatim) and drive-time (OSRM), both cached in the database."""

from __future__ import annotations

import logging
import math
import threading
import time

import httpx

from eventscout.config import Config
from eventscout.db import Database
from eventscout.models import Event

log = logging.getLogger(__name__)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class Geo:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.home = (cfg.home.latitude, cfg.home.longitude)
        self.client = httpx.Client(headers={"User-Agent": cfg.fetch.user_agent}, timeout=20)
        self._lock = threading.Lock()
        self._last = 0.0
        self.lookups = 0

    def _nominatim(self, query: str) -> tuple[float | None, float | None] | None:
        """(lat, lon), (None, None) for a definitive miss, or None when the lookup was skipped (retry next run)."""
        cached = self.db.get_geocode(query)
        if cached is not None:
            return cached
        if not self.cfg.geocoding.nominatim_url:
            return (None, None)
        if self.lookups >= self.cfg.geocoding.max_geocodes_per_run:
            return None
        with self._lock:  # Nominatim policy: max 1 request/second
            wait = 1.1 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.lookups += 1
            lat, lon = self.home
            params = {
                "q": query, "format": "jsonv2", "limit": 1, "countrycodes": self.cfg.home.country.lower(),
                "viewbox": f"{lon - 1},{lat + 1},{lon + 1},{lat - 1}", "bounded": 0,
            }
            try:
                resp = self.client.get(self.cfg.geocoding.nominatim_url.rstrip("/") + "/search", params=params)
                resp.raise_for_status()
                hits = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("geocode failed for %r: %s", query, exc)
                return None  # not cached: retry next run
        result = (float(hits[0]["lat"]), float(hits[0]["lon"])) if hits else (None, None)
        self.db.put_geocode(query, *result)
        return result

    def drive_minutes(self, lat: float, lon: float) -> float | None:
        if not self.cfg.geocoding.osrm_url:
            return None
        key = f"{lat:.3f},{lon:.3f}"
        cached = self.db.get_route(key)
        if cached is not None:
            return cached[0]
        hl, hn = self.home
        url = f"{self.cfg.geocoding.osrm_url.rstrip('/')}/route/v1/driving/{hn},{hl};{lon},{lat}"
        try:
            resp = self.client.get(url, params={"overview": "false"})
            resp.raise_for_status()
            routes = resp.json().get("routes") or []
            minutes = round(routes[0]["duration"] / 60, 1) if routes else None
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("routing failed for %s: %s", key, exc)
            return None
        self.db.put_route(key, minutes)
        return minutes

    def locate(self, ev: Event) -> dict:
        """Returns fields to update on the event: latitude/longitude/distance_miles/drive_minutes/location_approx."""
        lat, lon, approx = ev.latitude, ev.longitude, False
        if lat is None or lon is None:
            region = self.cfg.home.region
            queries = []
            if ev.address:
                queries.append(ev.address if (ev.city and ev.city in ev.address) or not ev.city else f"{ev.address}, {ev.city}")
            if ev.venue_name:
                queries.append(f"{ev.venue_name}, {ev.city or ''} {region}".replace(" ,", ","))
            if ev.city:
                queries.append(f"{ev.city}, {region}")
            for i, q in enumerate(queries):
                hit = self._nominatim(q.strip(", "))
                if hit is None:
                    return {}  # geocoding budget exhausted or service error: try again next run
                lat, lon = hit
                if lat is not None:
                    approx = bool(ev.city) and i == len(queries) - 1
                    break
        if lat is None or lon is None:
            return {"geo_attempted": 1}
        fields = {
            "latitude": lat, "longitude": lon, "location_approx": approx, "geo_attempted": 1,
            "distance_miles": round(haversine_miles(self.home[0], self.home[1], lat, lon), 1),
        }
        if not approx:
            fields["drive_minutes"] = self.drive_minutes(lat, lon)
        return fields
