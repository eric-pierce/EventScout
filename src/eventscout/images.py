"""Event images: find a page's preview image (og:image and friends). Images are linked, never downloaded."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from eventscout.urls import safe_urljoin

_META = (
    {"property": "og:image:secure_url"},
    {"property": "og:image"},
    {"name": "og:image"},
    {"name": "twitter:image"},
    {"property": "twitter:image"},
    {"name": "twitter:image:src"},
    {"itemprop": "image"},
)


def clean_image_url(url: object, base_url: str | None = None) -> str | None:
    """Only absolute http(s) URLs are allowed into <img src>; anything else (data:, javascript:, junk) is dropped."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or len(url) > 2000:
        return None
    if url.startswith("//"):
        url = "https:" + url
    if base_url:
        url = safe_urljoin(base_url, url)
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return url


def find_image(html: str, base_url: str) -> str | None:
    """Best preview image for a page: Open Graph / Twitter card meta, then image_src, then a JSON-LD image."""
    soup = BeautifulSoup(html, "lxml")
    for attrs in _META:
        tag = soup.find("meta", attrs=attrs)
        if tag and (url := clean_image_url(tag.get("content"), base_url)):
            return url
    link = soup.find("link", rel="image_src", href=True)
    if link and (url := clean_image_url(link["href"], base_url)):
        return url
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            image = node.get("image")
            image = image[0] if isinstance(image, list) and image else image
            if isinstance(image, dict):
                image = image.get("url")
            if url := clean_image_url(image, base_url):
                return url
    return None
