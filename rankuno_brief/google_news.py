"""Resolves Google News RSS links (news.google.com/rss/articles/<id>) to the publisher's article URL.

Google hides the destination behind an encoded id. Resolving it takes two requests: the article
page (which carries a signature and timestamp) and Google's internal batchexecute endpoint. This is
not an official API and may change, so every failure falls back to the Google News link, which
still takes readers to the article in a browser. It runs only for stories chosen for an issue.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit

import httpx

from . import text

log = logging.getLogger(__name__)

BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
# Google serves the signature only to regular browsers.
BROWSER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_SIGNATURE = re.compile(r'data-n-a-sg="([^"]+)"')
_TIMESTAMP = re.compile(r'data-n-a-ts="([^"]+)"')


def is_google_news_link(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.hostname or "") == "news.google.com" and "/articles/" in parts.path


def decode(client: httpx.Client, url: str) -> str | None:
    """The publisher URL behind a Google News link, or None if it cannot be resolved."""
    article_id = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    try:
        page = client.get(f"https://news.google.com/rss/articles/{article_id}")
        signature, timestamp = _SIGNATURE.search(page.text), _TIMESTAMP.search(page.text)
        if page.status_code >= 400 or not signature or not timestamp:
            return None
        request = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{article_id}",{timestamp.group(1)},"{signature.group(1)}"]'
        )
        response = client.post(
            BATCHEXECUTE_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            data={"f.req": json.dumps([[["Fbv4je", request]]])},
        )
        if response.status_code >= 400:
            return None
        envelope = json.loads(response.text.split("\n\n", 1)[1])[:-2]
        target = json.loads(envelope[0][2])[1]
    except (httpx.HTTPError, ValueError, IndexError, KeyError, TypeError) as exc:
        log.info("Could not resolve Google News link %s (%s)", url, type(exc).__name__)
        return None
    return target if isinstance(target, str) and target.startswith(("http://", "https://")) else None


def resolve_links(stories, delay_seconds: float = 1.0, timeout: float = 15.0, sleep=time.sleep) -> dict[int, str]:
    """Replace Google News links on stories (and their "Also reported by" links) with publisher URLs.

    Returns {item_id: resolved_url} for the stories' own links, so they can be stored.
    """
    pending = [story for story in stories if is_google_news_link(story.url)]
    pending_mentions = sum(1 for story in stories for _, url in story.also_reported_by if is_google_news_link(url))
    resolved: dict[int, str] = {}
    if not pending and not pending_mentions:
        return resolved

    attempts = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": BROWSER_USER_AGENT}) as client:

        def resolve(url: str) -> str | None:
            nonlocal attempts
            if attempts:
                sleep(delay_seconds)  # sequential and gentle: Google rate-limits bursts
            attempts += 1
            target = decode(client, url)
            return text.canonical_url(target) if target else None

        for story in pending:
            if target := resolve(story.url):
                story.url = resolved[story.item_id] = target
        for story in stories:
            story.also_reported_by = [
                (name, (resolve(url) or url) if is_google_news_link(url) else url) for name, url in story.also_reported_by
            ]
    log.info("Resolved %d of %d Google News story links (%d mentions checked)", len(resolved), len(pending), pending_mentions)
    return resolved
