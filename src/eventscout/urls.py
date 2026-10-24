"""URL helpers that tolerate the malformed links found on real web pages."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse


def safe_urljoin(base: str, url: str | None) -> str | None:
    """urljoin that returns None for links Python can't parse (e.g. "http://[broken") instead of raising."""
    if not url:
        return None
    try:
        joined = urljoin(base, url.strip())
        urlparse(joined)  # validates the result too
    except ValueError:
        return None
    return joined
