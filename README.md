# EventScout

A self-hosted service that periodically gathers local events from known sources and aggregators, discovers new
sources on its own, filters and ranks everything against your interests, and delivers the result as a web dashboard,
an email digest, and subscribable calendar (.ics) feeds.

Configured out of the box for Newton, MA / Greater Boston.

## How it works

```
 sources ──► fetch ──► normalize & dedupe ──► geocode + drive time ──► hard filters ──► LLM scoring ──► enrich ──► outputs
   ▲                                                                                        ▲                     │
   └── discovery (weekly): web search → LLM shortlist → validate → trial source             └─ your 👍/👎 feedback  ▼
                                                                          dashboard · email · calendar.ics · report.html
```

| Stage | What happens |
|---|---|
| **Fetch** | Each source has a *kind*. Structured kinds need no LLM: `localist` (university/city calendars API), `ical`, `ticketmaster` (API), `bostonshows`, and any page with schema.org JSON-LD (Eventbrite, Meetup, AllEvents…). Pages without structured data (`web`) are read by the LLM. RSS entries go through the LLM too. Robots.txt and per-domain pacing are respected. |
| **Dedupe** | The same event from several sources (same day, fuzzy-matched title, same venue, start within 90 minutes) is merged, keeping the richest fields and every source name. |
| **Locate** | Venues are geocoded with OpenStreetMap Nominatim and given drive times via OSRM. Both are cached in SQLite, so each venue is looked up once. |
| **Filter** | Cheap deterministic rules run first: distance, drive time, max price, online-only, and excluded keywords. |
| **Score** | The LLM rates each remaining listing 0–100 against your weighted interests, dislikes, notes and past 👍/👎, and gives a one-line reason. Identical listings (for example weekly recurring shows) share one cached score. Only new or changed listings are scored, so after the first run daily runs are cheap. |
| **Enrich** | For top picks missing a start time or price, the detail page is fetched and the gaps are filled. |
| **Lifecycle** | Discovered sources start as `trial`. A trial source is promoted to `active` once it yields an event scoring above your threshold, or retired after N runs without one. Any source is disabled after 5 consecutive failures. |
| **Outputs** | Dashboard, `/calendar.ics` (picks), `/calendar/starred.ics` (events you starred), a download per event, the static `data/output/report.html` + `events.ics` + `events.json`, and an optional HTML email with the .ics attached. |

## Quick start (Docker, on your home server)

```bash
cp config.example.yaml config.yaml     # edit profile.interests (the most important part)
cp .env.example .env                   # add ANTHROPIC_API_KEY, PUBLIC_BASE_URL, EVENTSCOUT_FEED_TOKEN, SMTP…
mkdir -p data && sudo chown 1000:1000 data   # container runs as uid 1000
docker compose up -d --build
docker compose exec eventscout eventscout run   # first run now instead of waiting for 6am
```

Open `http://<server>:8080`. Scheduled runs happen daily at 6am, and discovery runs Sundays at 5am. Both schedules are cron strings in `config.yaml`.

**Calendar subscription:** copy the *Picks* feed URL from the dashboard sidebar into Google Calendar ("From URL"),
Apple Calendar ("New Calendar Subscription"), or Outlook. If `EVENTSCOUT_FEED_TOKEN` is set, the URL includes
`?token=…`. Calendar apps have to reach the server, so either use them on your LAN/VPN (e.g. Tailscale) or put the
app behind a reverse proxy with HTTPS. Google Calendar fetches from Google's servers, so it needs a publicly reachable URL.

### Local LLM instead of Claude

```bash
docker compose --profile local-llm up -d          # starts ollama + searxng
docker compose exec ollama ollama pull qwen3:14b
```
Set `llm.provider: ollama` in `config.yaml`. Discovery then uses the bundled SearXNG for web search.
Use a 14B+ model that handles JSON well. Smaller models extract noticeably worse.

### JS-rendered calendars

`docker compose --profile browser up -d`, set `BROWSERLESS_URL=http://browserless:3000`, then add
`options: { render: true }` to the source.

## Configuration highlights (`config.yaml`)

- `profile.interests`: weighted topics that drive ranking. Changing the profile re-scores upcoming events.
- `profile.dislikes`, `profile.notes`: soft preferences, e.g. "weeknights only after 6pm".
- `profile.exclude_keywords`: hard filter.
- `profile.min_score`: threshold for picks, email and the calendar feed.
- `search`: `horizon_days`, `max_distance_miles`, `max_drive_minutes`, `max_price`, `include_online`.
- `llm.claude.model`: defaults to `claude-opus-5` with low effort for extraction and scoring, high for discovery. For a cheaper setup, set `claude-sonnet-5`.
- `llm.max_scored_per_run`: cost guard. The soonest events are scored first; the rest wait for the next run.
- `sources`: seed list. The kind can be `auto`, which detects iCal / RSS / Localist / JSON-LD / LLM automatically.

Add sources from the dashboard (Sources → Add source), or with the CLI:

```bash
docker compose exec eventscout eventscout sources add https://www.somervilleartscouncil.org/events --name "Somerville Arts"
docker compose exec eventscout eventscout sources check all      # dry-run every source, print sample events
```

## Cost (Claude)

The first run scores every upcoming listing in the window, roughly 1.5–2k unique listings with the default Boston
seeds. The run history page shows exact token counts per run. After that, daily runs only score new listings
(typically a few hundred). LLM page extraction only happens for sources without structured data, and is cached by
content hash, so unchanged pages cost nothing. New 👍/👎 feedback shapes how new listings are scored. Run
`eventscout rescore` to re-rank everything already stored.
Levers: `llm.claude.model`, `search.horizon_days`, `llm.max_scored_per_run`, disabling high-volume sources you don't care
about (e.g. university calendars), and `profile.exclude_keywords`.

## CLI

```
eventscout serve                  # dashboard + scheduler (container default)
eventscout run [--discover] [--no-email]
eventscout discover               # find new sources only
eventscout report [--email]       # rebuild outputs from stored data
eventscout rescore                # re-rank everything on the next run (e.g. after lots of 👍/👎)
eventscout sources list|add URL|check [ID|all]
eventscout test-llm               # sanity-check the LLM provider
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
EVENTSCOUT_DATA=./data .venv/bin/eventscout -c config.yaml serve
```

Layout: `sources/` (one adapter per kind) · `llm/` (Claude + Ollama behind one interface) · `tasks.py` (prompts
and schemas) · `pipeline.py` (the run) · `discovery.py` · `geo.py` · `report/` (HTML/ICS/email) · `web/` (FastAPI
dashboard).

## Notes on specific sources

- **thebostoncalendar.com** sits behind Cloudflare's bot challenge. It sometimes lets the app's client through. When it
  doesn't, the run log says so, and after 5 blocked runs the source is disabled automatically. EventScout does not try
  to get around bot protection.
- **do617** renders its listings with JavaScript, so it needs the headless browser: run
  `docker compose --profile browser up -d` with `BROWSERLESS_URL`/`BROWSERLESS_TOKEN` set in `.env`. The source
  reads do617's per-category "today" pages, so each daily run picks up that day's concerts, comedy, food/drink,
  trivia and theatre listings.
- **bostonshows.org** is one large static page (1,000+ shows over three weeks), so it has a dedicated parser rather
  than going through the LLM.
- **Eventbrite / AllEvents** listing pages only give dates. Top picks get their start time and price from the detail
  page during the enrich step.
