import os
import subprocess
from pathlib import Path

import pytest

from eventscout.config import load_config
from eventscout.filesecrets import SecretFileError, load_file_secrets, secrets_report

REPO = Path(__file__).resolve().parent.parent


def test_file_secret_is_loaded_and_stripped(tmp_path):
    (tmp_path / "key").write_text("sk-ant-test\n")
    env = {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "key")}
    assert load_file_secrets(env) == ["ANTHROPIC_API_KEY"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert load_file_secrets(env) == ["ANTHROPIC_API_KEY"]  # idempotent: a second load is fine
    assert secrets_report(env)["ANTHROPIC_API_KEY"] == "file"


def test_empty_file_means_unset(tmp_path):
    (tmp_path / "pw").write_text("")
    env = {"SMTP_PASSWORD_FILE": str(tmp_path / "pw"), "SMTP_PASSWORD": ""}  # env_file passes empty strings
    assert load_file_secrets(env) == []
    assert not env.get("SMTP_PASSWORD")
    assert secrets_report(env)["SMTP_PASSWORD"] == "unset (empty file)"


def test_conflicting_env_and_file_is_an_error(tmp_path):
    (tmp_path / "key").write_text("from-file")
    env = {"ANTHROPIC_API_KEY_FILE": str(tmp_path / "key"), "ANTHROPIC_API_KEY": "from-env"}
    with pytest.raises(SecretFileError, match="both ANTHROPIC_API_KEY and ANTHROPIC_API_KEY_FILE"):
        load_file_secrets(env)


def test_missing_file_names_the_variable(tmp_path):
    with pytest.raises(SecretFileError, match="WEB_PASSWORD_FILE points to .*nope"):
        load_file_secrets({"WEB_PASSWORD_FILE": str(tmp_path / "nope")})


def test_config_interpolates_file_secrets(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("feedtok123\n")
    (tmp_path / "config.yaml").write_text("report:\n  feed_token: ${EVENTSCOUT_FEED_TOKEN:-}\n")
    monkeypatch.setenv("EVENTSCOUT_FEED_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.delenv("EVENTSCOUT_FEED_TOKEN", raising=False)
    monkeypatch.setenv("EVENTSCOUT_DATA", str(tmp_path / "data"))
    assert load_config(tmp_path / "config.yaml").report.feed_token == "feedtok123"
    os.environ.pop("EVENTSCOUT_FEED_TOKEN", None)  # load_file_secrets writes os.environ directly


def test_init_script_moves_values_out_of_env(tmp_path):
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    (work / "scripts" / "init-secrets.sh").write_bytes((REPO / "scripts" / "init-secrets.sh").read_bytes())
    (work / ".env").write_text('ANTHROPIC_API_KEY="sk-ant-abc"\nWEB_USERNAME=me\nSMTP_PASSWORD=\n')
    out = subprocess.run(["sh", "scripts/init-secrets.sh", "--from-env"], cwd=work, capture_output=True, text=True,
                         check=True).stdout
    assert "sk-ant-abc" not in out  # values are never printed
    assert (work / "secrets" / "anthropic_api_key").read_text() == "sk-ant-abc"
    assert (work / "secrets" / "smtp_password").read_text() == ""
    assert "ANTHROPIC_API_KEY=\n" in (work / ".env").read_text() and "WEB_USERNAME=me" in (work / ".env").read_text()
    assert oct((work / "secrets").stat().st_mode & 0o777) == "0o700"
    assert oct((work / "secrets" / "anthropic_api_key").stat().st_mode & 0o777) == "0o600"
