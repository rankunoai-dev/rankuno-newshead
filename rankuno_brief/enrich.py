"""Fills in a preview image and a summary for selected stories whose source did not supply them.

Reads only the <head> of each article page (og:image, og:description and friends). It runs on the
stories chosen for an issue, so it costs a couple of dozen page requests per build.
"""

from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from . import text

log = logging.getLogger(__name__)

MAX_HEAD_BYTES = 400_000
MIN_USEFUL_EXCERPT = 40
_META_TAG = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_META_KEY = re.compile(r"\b(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_CONTENT_ATTR = re.compile(r"\bcontent\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE)
_IMAGE_KEYS = ("og:image", "og:image:secure_url", "twitter:image", "twitter:image:src")
_DESCRIPTION_KEYS = ("og:description", "twitter:description", "description")


@dataclass(frozen=True)
class PageMeta:
    image_url: str | None = None
    description: str | None = None


def parse_head(head: str, base_url: str) -> PageMeta:
    values: dict[str, str] = {}
    for tag in _META_TAG.finditer(head):
        key, content = _META_KEY.search(tag.group(0)), _CONTENT_ATTR.search(tag.group(0))
        if key and content:
            values.setdefault(key.group(1).lower(), html.unescape(content.group(1) or content.group(2) or "").strip())

    image = None
    for key in _IMAGE_KEYS:
        if values.get(key):
            candidate = urljoin(base_url, values[key])
            if text.usable_image_url(candidate):
                image = candidate
                break
    description = next((values[key] for key in _DESCRIPTION_KEYS if values.get(key)), None)
    return PageMeta(image_url=image, description=text.fix_mojibake(description) if description else None)


def fetch_page_meta(client: httpx.Client, url: str) -> PageMeta:
    try:
        with client.stream("GET", url) as response:
            if response.status_code >= 400 or "html" not in response.headers.get("content-type", ""):
                return PageMeta()
            received = bytearray()
            for chunk in response.iter_bytes():
                received.extend(chunk)
                if len(received) >= MAX_HEAD_BYTES or b"</head>" in received[-len(chunk) - 8 :].lower():
                    break
            head = received.decode(response.encoding or "utf-8", errors="ignore")
            base_url = str(response.url)
    except httpx.HTTPError as exc:
        log.info("Could not read %s (%s)", url, type(exc).__name__)
        return PageMeta()
    return parse_head(head, base_url)


def fill_missing_details(stories: list, user_agent: str, timeout: float = 10.0) -> dict[int, dict[str, str]]:
    """Set image_url and excerpt on stories that lack them. Returns {item_id: {field: value}} for storage."""
    incomplete = [story for story in stories if not story.image_url or len(story.excerpt) < MIN_USEFUL_EXCERPT]
    if not incomplete:
        return {}
    updates: dict[int, dict[str, str]] = {}
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for story, meta in zip(incomplete, pool.map(lambda story: fetch_page_meta(client, story.url), incomplete)):
                fields: dict[str, str] = {}
                if not story.image_url and meta.image_url:
                    story.image_url = fields["image_url"] = meta.image_url
                if len(story.excerpt) < MIN_USEFUL_EXCERPT and meta.description:
                    excerpt = text.make_excerpt(meta.description)
                    if len(excerpt) > len(story.excerpt):
                        story.excerpt = fields["excerpt"] = excerpt
                if fields:
                    updates[story.item_id] = fields
    return updates
