import psycopg

from eventscout.db import Database
from eventscout.http import FetchResult
from eventscout.images import clean_image_url, find_image
from eventscout.models import RawEvent
from eventscout.pipeline import Service

from conftest import FakeLLM, at


def test_find_image_prefers_og_and_resolves_relative():
    html = '<head><meta name="twitter:image" content="/t.jpg"><meta property="og:image" content="/img/show.jpg"></head>'
    assert find_image(html, "https://club.example/events/1") == "https://club.example/img/show.jpg"


def test_find_image_falls_back_to_jsonld():
    html = '<script type="application/ld+json">{"@type": "Event", "image": [{"url": "https://cdn.example/a.png"}]}</script>'
    assert find_image(html, "https://x.example") == "https://cdn.example/a.png"
    assert find_image("<html><body>no image</body></html>", "https://x.example") is None


def test_unsafe_image_urls_are_rejected():
    assert clean_image_url("javascript:alert(1)") is None
    assert clean_image_url("data:image/png;base64,AAAA") is None
    assert clean_image_url("//cdn.example/p.jpg") == "https://cdn.example/p.jpg"
    assert RawEvent(title="x", start=at(1), image_url="javascript:alert(1)").image_url is None


def test_image_step_links_page_images_and_drops_logos(cfg, fake_adapter, monkeypatch):
    fake_adapter.events = [
        RawEvent(title="Jazz Night", start=at(1), url="https://club.example/jazz"),
        RawEvent(title="Jazz Brunch", start=at(2), url="https://club.example/brunch", image_url="https://cdn.example/own.jpg"),
        # Four different events whose pages all carry the same site logo.
        *[RawEvent(title=f"Jazz Series {n}", start=at(n), url=f"https://venue.example/{n}") for n in range(1, 5)],
    ]
    pages = {"https://club.example/jazz": '<meta property="og:image" content="https://cdn.example/jazz.jpg">'}

    def fake_get(self, url, **kw):
        body = pages.get(url, '<meta property="og:image" content="https://venue.example/logo.png">')
        return FetchResult(url=url, status=200, content_type="text/html", text=body, content=body.encode())

    monkeypatch.setattr("eventscout.http.Fetcher.get", fake_get)
    svc = Service(cfg)
    svc._llm = FakeLLM()
    stats = svc.run()

    imgs = {e.title: e.image_url for e in svc.db.upcoming_events(at(-1), at(10))}
    assert imgs["Jazz Night"] == "https://cdn.example/jazz.jpg"      # from the event page
    assert imgs["Jazz Brunch"] == "https://cdn.example/own.jpg"      # source image kept, page not fetched
    assert all(imgs[f"Jazz Series {n}"] is None for n in range(1, 5))  # shared logo dropped
    assert stats["images"]["generic_dropped"] == 4

    html = open(f"{cfg.report.output_dir}/report.html").read()
    assert 'src="https://cdn.example/jazz.jpg"' in html and 'class="thumb empty"' in html


def test_migration_adds_image_checked(db_config):
    """A database created before image_checked existed gets the column on startup."""
    from eventscout.db import SCHEMA

    with psycopg.connect(db_config.url, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{db_config.schema_name}"')
        c.execute(f'SET search_path TO "{db_config.schema_name}"')
        c.execute(SCHEMA.replace("    image_checked BOOLEAN NOT NULL DEFAULT false,\n", ""))
        assert "image_checked" not in _columns(c, db_config.schema_name)
    Database(db_config, "UTC")
    with psycopg.connect(db_config.url) as c:
        assert "image_checked" in _columns(c, db_config.schema_name)


def _columns(conn, schema: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'events'", (schema,)
    ).fetchall()
    return {r[0] for r in rows}
