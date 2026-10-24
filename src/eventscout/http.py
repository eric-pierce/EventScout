"""Polite HTTP fetching: robots.txt, per-domain pacing, optional headless rendering via browserless."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from eventscout.config import FetchConfig


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    """The site actively blocks automated access (bot challenge / robots.txt)."""


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    text: str
    content: bytes

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or self.text.lstrip()[:200].lower().startswith(("<!doctype html", "<html"))


class Fetcher:
    def __init__(self, cfg: FetchConfig):
        self.cfg = cfg
        self.client = httpx.Client(
            headers={
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,text/calendar;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            },
            timeout=cfg.timeout_seconds,
            follow_redirects=True,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._domain_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------------
    def _domain_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._domain_locks.setdefault(host, threading.Lock())

    def _pace(self, host: str) -> None:
        delay = self.cfg.per_domain_delay_seconds
        last = self._last_hit.get(host)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                resp = self.client.get(urljoin(base, "/robots.txt"))
                if resp.status_code >= 400:
                    rp = None  # no robots.txt: everything allowed
                else:
                    rp.parse(resp.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.cfg.user_agent, url)

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        render: bool = False,
        check_robots: bool = True,
        headers: dict | None = None,
    ) -> FetchResult:
        try:
            host = urlparse(url).netloc
        except ValueError as exc:
            raise FetchError(f"malformed URL {url!r}: {exc}") from exc
        if check_robots and not self.allowed(url):
            raise BlockedError(f"robots.txt disallows {url}")
        with self._domain_lock(host):
            self._pace(host)
            if render and self.cfg.browserless_url:
                return self._render(url)
            try:
                resp = self.client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if resp.headers.get("cf-mitigated") == "challenge" or (
            resp.status_code in (403, 503) and "Just a moment..." in resp.text[:2000]
        ):
            raise BlockedError(f"{host} is behind a Cloudflare bot challenge; it can't be fetched automatically")
        if resp.status_code >= 400:
            raise FetchError(f"HTTP {resp.status_code} for {resp.url}")
        return FetchResult(
            url=str(resp.url),
            status=resp.status_code,
            content_type=resp.headers.get("content-type", "").lower(),
            text=resp.text,
            content=resp.content,
        )

    def _render(self, url: str) -> FetchResult:
        endpoint = self.cfg.browserless_url.rstrip("/") + "/content"
        params = {"token": self.cfg.browserless_token} if self.cfg.browserless_token else None
        try:
            resp = self.client.post(
                endpoint,
                params=params,
                json={"url": url, "gotoOptions": {"waitUntil": "networkidle2", "timeout": 45000}},
                timeout=90,
            )
        except httpx.HTTPError as exc:
            raise FetchError(f"browserless: {exc}") from exc
        if resp.status_code >= 400:
            raise FetchError(f"browserless HTTP {resp.status_code}: {resp.text[:200]}")
        return FetchResult(url=url, status=200, content_type="text/html", text=resp.text, content=resp.content)
