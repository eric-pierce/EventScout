import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eventscout.config import Interest, Profile
from eventscout.configfile import ConfigFile, ConfigFileError, text_hash
from eventscout.detect import Detection
from eventscout.pipeline import Service
from eventscout.web.app import create_app

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"

SMALL = """\
# My EventScout config
home:
  label: "Newton, MA"   # where I live
profile:
  min_score: 60
  interests:
    - {topic: "jazz", weight: 5}
  dislikes:
    - networking mixers
  exclude_keywords: []
  notes: |
    Weeknights after 6pm.
email:
  password: ${SMTP_PASSWORD:-}   # from secrets/smtp_password
sources:
  - name: Boston Shows
    url: https://bostonshows.org/
    kind: bostonshows
"""


@pytest.fixture
def cfile(tmp_path) -> ConfigFile:
    path = tmp_path / "config.yaml"
    path.write_text(SMALL)
    return ConfigFile(path, tmp_path / "backups")


def test_profile_update_keeps_comments_placeholders_and_other_sections(cfile):
    cfile.update_profile(Profile(
        interests=[Interest(topic="small-venue jazz", weight=5), Interest(topic="comedy", weight=3)],
        dislikes=["kids' events"], exclude_keywords=["webinar"], notes="Weekends anytime.", min_score=55,
    ))
    text = cfile.read_text()
    assert "# My EventScout config" in text and "# where I live" in text and "# from secrets/smtp_password" in text
    assert "${SMTP_PASSWORD:-}" in text                            # never interpolated into the file
    assert '- {topic: "small-venue jazz", weight: 5}' in text      # one flow-style line per interest
    assert "notes: |\n    Weekends anytime.\n" in text            # literal block kept
    assert "kind: bostonshows" in text
    assert len(list((cfile.backup_dir).glob("config-*.yaml"))) == 1
    assert (next(cfile.backup_dir.glob("config-*.yaml"))).read_text() == SMALL


def test_sources_add_disable_enable_remove(cfile):
    cfile.add_source("Somerville Arts", "https://arts.example/events", "web", {"render": True})
    assert "name: Somerville Arts" in cfile.read_text() and "render: true" in cfile.read_text()
    assert cfile.set_source_enabled("https://arts.example/events/", False)  # trailing slash tolerated
    assert "enabled: false" in cfile.read_text()
    assert cfile.set_source_enabled("https://arts.example/events", True)
    assert "enabled:" not in cfile.read_text()
    assert cfile.remove_source("https://arts.example/events")
    assert "Somerville Arts" not in cfile.read_text() and "Boston Shows" in cfile.read_text()


def test_unknown_source_leaves_file_untouched(cfile):
    before = cfile.path.stat().st_mtime_ns
    assert cfile.set_source_enabled("https://nope.example", False) is False
    assert cfile.remove_source("https://nope.example") is False
    assert cfile.path.stat().st_mtime_ns == before and cfile.read_text() == SMALL


def test_invalid_config_is_rejected_and_file_kept(cfile):
    with pytest.raises(ConfigFileError, match="invalid configuration: profile.min_score"):
        cfile.write_text(SMALL.replace("min_score: 60", "min_score: lots"), base_hash=text_hash(SMALL))
    with pytest.raises(ConfigFileError, match="YAML syntax error"):
        cfile.write_text("profile: [unclosed", base_hash=text_hash(SMALL))
    assert cfile.read_text() == SMALL


def test_raw_save_refuses_to_overwrite_newer_edits(cfile):
    stale = text_hash(SMALL)
    cfile.path.write_text(SMALL + "\n# edited in vim meanwhile\n")
    with pytest.raises(ConfigFileError, match="changed on disk"):
        cfile.write_text(SMALL, base_hash=stale)


def test_read_only_file_explains_how_to_fix(cfile):
    os.chmod(cfile.path, 0o444)
    try:
        if os.access(cfile.path, os.W_OK):
            pytest.skip("running as root; can't make the file read-only")
        assert "chown 1000" in cfile.read_only_reason()
        with pytest.raises(ConfigFileError, match="read-only"):
            cfile.update_profile(Profile(min_score=10))
    finally:
        os.chmod(cfile.path, 0o644)


def test_example_config_round_trips_unchanged(tmp_path):
    import yaml

    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text())
    cf = ConfigFile(path)
    profile = Profile.model_validate(yaml.safe_load(EXAMPLE.read_text())["profile"])
    assert cf.update_profile(profile) is None and path.read_text() == EXAMPLE.read_text()  # unchanged: not rewritten
    # One real change touches only that line.
    cf.update_profile(profile.model_copy(update={"min_score": 65}))
    changed = [(a, b) for a, b in zip(EXAMPLE.read_text().splitlines(), path.read_text().splitlines()) if a != b]
    assert len(changed) == 1 and changed[0][1].strip().startswith("min_score: 65")


# ----------------------------------------------------------------------------- web


@pytest.fixture
def app_env(db_config, fake_adapter, tmp_path, monkeypatch):
    """Like production: the running config is loaded from the same file the UI edits."""
    from eventscout.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        SMALL.replace("kind: bostonshows", "kind: bostonshows\n    enabled: false")
        + f"database:\n  url: {db_config.url}\n  schema: {db_config.schema_name}\n  connect_timeout: 5\n"
        + "geocoding:\n  nominatim_url: null\n  osrm_url: null\n"
    )
    monkeypatch.setenv("EVENTSCOUT_DATA", str(tmp_path / "data"))
    monkeypatch.setattr("eventscout.web.app.detect_kind", lambda url, fetcher, ctx: Detection("web", url, "test"))
    monkeypatch.setattr("eventscout.web.app.trial_fetch", lambda det, name, ctx: [])
    svc = Service(load_config(path), config_path=path)
    return svc, TestClient(create_app(svc)), path


def test_settings_page_and_profile_save_apply_live(app_env):
    svc, client, path = app_env
    page = client.get("/settings")
    assert page.status_code == 200 and "networking mixers" in page.text and "${SMTP_PASSWORD:-}" in page.text
    r = client.post("/api/profile", json={"interests": [{"topic": "folk", "weight": 4}], "dislikes": [],
                                          "exclude_keywords": ["toddler"], "notes": "", "min_score": 70})
    assert r.status_code == 200 and r.json()["rescore"] is True
    assert svc.cfg.profile.min_score == 70 and svc.cfg.profile.interests[0].topic == "folk"  # applied without restart
    assert '{topic: "folk", weight: 4}' in path.read_text()
    assert client.post("/api/profile", json={"interests": [{"topic": "x", "weight": 9}]}).status_code == 422


def test_raw_config_save(app_env):
    svc, client, path = app_env
    text = path.read_text()
    r = client.post("/api/config", json={"text": text.replace('label: "Newton, MA"', 'label: "Newton Centre, MA"'),
                                         "base_hash": text_hash(text)})
    assert r.status_code == 200 and r.json()["restart_needed"] == []
    assert svc.cfg.home.label == "Newton Centre, MA"
    bad = client.post("/api/config", json={"text": "home: [", "base_hash": r.json()["hash"]})
    assert bad.status_code == 422 and "YAML" in bad.json()["detail"]


def test_sources_added_in_ui_are_written_to_config(app_env):
    svc, client, path = app_env
    r = client.post("/api/sources", json={"url": "https://newsletter.example/", "name": "Newsletter"})
    assert r.status_code == 200 and r.json()["saved_to_config"] is True
    assert "url: https://newsletter.example/" in path.read_text()
    src = next(s for s in svc.db.list_sources() if s.url == "https://newsletter.example/")
    assert src.origin == "seed"

    assert client.post(f"/api/sources/{src.id}/status", json={"status": "disabled"}).json()["warning"] is None
    assert "enabled: false" in path.read_text().split("Newsletter", 1)[1]
    assert client.delete(f"/api/sources/{src.id}").status_code == 200
    assert "Newsletter" not in path.read_text()
    assert all(s.url != "https://newsletter.example/" for s in svc.db.list_sources())
