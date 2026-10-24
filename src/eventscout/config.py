"""Configuration: a YAML file (with ${ENV_VAR} interpolation) validated by pydantic."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _interpolate(value: Any) -> Any:
    """Replace ${VAR} / ${VAR:-default} in strings with environment values."""
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) or "")

        out = _ENV_PATTERN.sub(sub, value)
        return out if out != "" or value == "" else None
    if isinstance(value, list):
        return [v for v in (_interpolate(v) for v in value) if v is not None]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name) or default


class DatabaseConfig(BaseModel):
    """PostgreSQL connection. Every field defaults to the matching POSTGRES_* environment variable (the password
    usually arrives via POSTGRES_PASSWORD_FILE, see filesecrets), so config.yaml rarely needs this section."""

    model_config = ConfigDict(populate_by_name=True)

    url: str | None = Field(default_factory=lambda: _env("DATABASE_URL"))  # full libpq URL; overrides the fields below
    host: str = Field(default_factory=lambda: _env("POSTGRES_HOST", "postgres"))
    port: int = Field(default_factory=lambda: int(_env("POSTGRES_PORT", "5432")))
    name: str = Field(default_factory=lambda: _env("POSTGRES_DB", "eventscout"))
    user: str = Field(default_factory=lambda: _env("POSTGRES_USER", "eventscout"))
    password: str | None = Field(default_factory=lambda: _env("POSTGRES_PASSWORD"), repr=False)
    schema_name: str = Field("public", alias="schema")
    pool_size: int = 8
    connect_timeout: float = 60  # seconds to wait for the server at startup (it may still be booting)


class Home(BaseModel):
    label: str = "Newton, MA"
    latitude: float = 42.3370
    longitude: float = -71.2092
    timezone: str = "America/New_York"
    city: str = "Newton"
    region: str = "Massachusetts"
    country: str = "US"
    # Nearby places the LLM should treat as "local" when researching sources.
    nearby: list[str] = Field(default_factory=lambda: ["Boston", "Cambridge", "Somerville", "Brookline", "Waltham"])


class SearchWindow(BaseModel):
    horizon_days: int = 21
    max_distance_miles: float = 30
    max_drive_minutes: float | None = None
    include_online: bool = False
    max_price: float | None = None


class Interest(BaseModel):
    topic: str
    weight: int = Field(3, ge=1, le=5)


class Profile(BaseModel):
    interests: list[Interest] = Field(default_factory=list)
    dislikes: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    notes: str = ""
    min_score: int = 60

    def fingerprint(self) -> str:
        """Changes whenever the profile changes, so events get re-scored."""
        blob = json.dumps(self.model_dump(), sort_keys=True)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-5"
    effort_extract: str = "low"
    effort_score: str = "low"
    effort_discover: str = "high"
    # Server-side refusal fallback ("default" lets Anthropic pick); set to null to disable.
    fallbacks: str | None = "default"
    max_web_searches: int = 12


class OllamaConfig(BaseModel):
    base_url: str = "http://ollama:11434"
    model: str = "qwen3:14b"
    num_ctx: int = 32768
    think: bool = False
    timeout_seconds: float = 900


class LLMConfig(BaseModel):
    provider: Literal["claude", "ollama"] = "claude"
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    score_batch_size: int = 20
    # Cost guard: at most this many unique listings are scored per run (soonest first); the rest wait for the next run.
    max_scored_per_run: int = 1500
    # Characters of page text per extraction call; long pages are split into several calls.
    extract_chunk_chars: int = 24000


class DiscoveryConfig(BaseModel):
    enabled: bool = True
    # auto: Claude web search when provider is claude, else SearXNG.
    search_backend: Literal["auto", "claude", "searxng"] = "auto"
    searxng_url: str | None = "http://searxng:8080"
    schedule: str = "0 5 * * 0"
    max_candidates_per_run: int = 10
    min_events_to_accept: int = 2
    trial_runs: int = 3
    blocked_domains: list[str] = Field(default_factory=lambda: ["facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "yelp.com", "tripadvisor.com"])


class FetchConfig(BaseModel):
    user_agent: str = "EventScout/0.1 (self-hosted personal event finder)"
    respect_robots: bool = True
    timeout_seconds: float = 30
    per_domain_delay_seconds: float = 1.5
    max_llm_chunks_per_page: int = 4
    concurrency: int = 6
    # Optional headless browser (browserless/chromium) for JS-rendered pages: sources with render: true.
    browserless_url: str | None = None
    browserless_token: str | None = None


class GeoConfig(BaseModel):
    nominatim_url: str | None = "https://nominatim.openstreetmap.org"
    osrm_url: str | None = "https://router.project-osrm.org"
    max_geocodes_per_run: int = 200


class ScheduleConfig(BaseModel):
    run: str = "0 6 * * *"
    run_on_startup: bool = False


class ReportConfig(BaseModel):
    output_dir: str = "/data/output"
    public_base_url: str = "http://localhost:8080"
    feed_token: str | None = None
    max_events: int = 200
    enrich_per_run: int = 25
    # Picks without an image get one from their event page's preview image (og:image); 0 disables.
    images_per_run: int = 80


class EmailConfig(BaseModel):
    enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    use_ssl: bool = False
    sender: str | None = None
    recipients: list[str] = Field(default_factory=list)
    subject: str = "EventScout: {count} events picked for you"
    only_new: bool = False

    @field_validator("recipients", mode="before")
    @classmethod
    def _split_recipients(cls, v: Any) -> list[str]:
        items = v if isinstance(v, list) else [v] if v else []
        return [addr.strip() for item in items for addr in str(item).split(",") if addr.strip()]


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    username: str | None = None
    password: str | None = None


class SourceSeed(BaseModel):
    name: str
    url: str
    kind: Literal["auto", "web", "jsonld", "ical", "rss", "localist", "ticketmaster", "bostonshows"] = "auto"
    options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class Config(BaseModel):
    data_dir: str = "/data"  # report outputs (report.html, events.ics, events.json); the data itself is in Postgres
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    home: Home = Field(default_factory=Home)
    search: SearchWindow = Field(default_factory=SearchWindow)
    profile: Profile = Field(default_factory=Profile)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    geocoding: GeoConfig = Field(default_factory=GeoConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    sources: list[SourceSeed] = Field(default_factory=list)


def default_config_path() -> Path:
    return Path(os.environ.get("EVENTSCOUT_CONFIG", "/config/config.yaml"))


def load_config(path: str | Path | None = None) -> Config:
    from eventscout.filesecrets import load_file_secrets

    load_file_secrets()  # NAME_FILE -> NAME, before ${NAME} interpolation and before any SDK reads the env
    path = Path(path) if path else default_config_path()
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    cfg = Config.model_validate(_interpolate(raw))
    if data_dir := os.environ.get("EVENTSCOUT_DATA"):
        cfg.data_dir = data_dir
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    return cfg
