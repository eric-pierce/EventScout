"""File-based secrets (Docker Compose / Swarm / Kubernetes style).

For any environment variable NAME, setting NAME_FILE=/path/to/file makes EventScout read NAME from that file.
Docker Compose mounts secrets at /run/secrets/<name>, so the compose file sets e.g.
ANTHROPIC_API_KEY_FILE=/run/secrets/anthropic_api_key and the key itself never appears in .env, the container's
environment, or `docker inspect`.

Rules: surrounding whitespace / trailing newline is stripped; an empty file means "not set" (so optional secrets can
exist as empty placeholder files); setting both NAME and NAME_FILE is an error, as it is for the official database
images, so there is never doubt about which value is in use.
"""

from __future__ import annotations

import os
from pathlib import Path

SUFFIX = "_FILE"
# Variables that must never be set directly in a container that uses secret files (checked by `secrets_report`).
SENSITIVE = (
    "ANTHROPIC_API_KEY",
    "SMTP_PASSWORD",
    "WEB_PASSWORD",
    "EVENTSCOUT_FEED_TOKEN",
    "TICKETMASTER_API_KEY",
)


class SecretFileError(RuntimeError):
    pass


def load_file_secrets(environ: dict[str, str] | None = None) -> list[str]:
    """Resolve every NAME_FILE variable into NAME. Returns the names loaded (never their values)."""
    env = os.environ if environ is None else environ
    loaded = []
    for key in sorted(k for k in list(env) if k.endswith(SUFFIX) and len(k) > len(SUFFIX)):
        name, path = key[: -len(SUFFIX)], env[key].strip()
        if not path:
            continue
        try:
            value = Path(path).read_text().strip()
        except FileNotFoundError:
            raise SecretFileError(f"{key} points to {path}, which does not exist") from None
        except PermissionError:
            raise SecretFileError(
                f"{key}: cannot read {path} (permission denied). The container runs as uid 1000: on the host run "
                f"`sudo chown 1000 secrets/*` and keep them `chmod 600`"
            ) from None
        if env.get(name) and env[name] != value:  # equal means an earlier call already loaded it
            raise SecretFileError(f"both {name} and {key} are set; remove {name} (keep secrets in the file only)")
        if value:
            env[name] = value
            loaded.append(name)
    return loaded


def secrets_report(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Where each sensitive setting comes from: 'file', 'env' or 'unset'. For logs / `eventscout check`; no values."""
    env = os.environ if environ is None else environ
    out = {}
    for name in SENSITIVE:
        if env.get(name + SUFFIX):
            out[name] = "file" if env.get(name) else "unset (empty file)"
        elif env.get(name):
            out[name] = "env"
        else:
            out[name] = "unset"
    return out
