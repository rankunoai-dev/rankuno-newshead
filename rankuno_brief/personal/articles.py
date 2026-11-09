"""Reads the full text of articles for the client brief: their main text, and the links they contain.

The text drives the key takeaways, the "where the client is mentioned" check and the competitor watch. Pages are fetched
once and cached in the database: a successful read is kept for CACHE_DAYS, a failed one is retried after
RETRY_DAYS. Each run reads at most `limit` pages, with a pause between requests to the same host.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urljoin

import httpx

from .. import db, text

log = logging.getLogger(__name__)

CACHE_DAYS = 30
RETRY_DAYS = 7
MAX_BODY_CHARS = 20_000
MAX_PAGE_BYTES = 2_000_000
MIN_BODY_CHARS = 80  # a page with less text than this is not an article (redirects, consent screens, empty shells)
SAME_HOST_DELAY_SECONDS = 2.0
_ARTICLE_BLOCK = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_HREF = re.compile(r"<a\b[^>]*?\bhref\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


@dataclass(frozen=True)
class ArticleText:
    body: str  # the main text, plain, truncated
    links: tuple[str, ...]  # absolute addresses of every link on the page


def extract(markup: str, base_url: str) -> ArticleText:
    """The article's main text (inside <article> or <main> when present) and all of its links."""
    block = markup
    found = _ARTICLE_BLOCK.search(markup)
    if found:
        block = found.group(2)
    body = text.html_to_text(block)[:MAX_BODY_CHARS]
    links = tuple(sorted({urljoin(base_url, href) for href in _HREF.findall(markup)}))
    return ArticleText(body=body, links=links)


def fetch(client: httpx.Client, url: str) -> ArticleText | None:
    """One article page, or None when it cannot be read as HTML."""
    try:
        with client.stream("GET", url) as response:
            if response.status_code >= 400 or "html" not in response.headers.get("content-type", ""):
                return None
            received = bytearray()
            for chunk in response.iter_bytes():
                received.extend(chunk)
                if len(received) >= MAX_PAGE_BYTES:
                    break
            markup = received.decode(response.encoding or "utf-8", errors="ignore")
            base_url = str(response.url)
    except httpx.HTTPError as exc:
        log.info("Could not read article %s (%s)", url, type(exc).__name__)
        return None
    article = extract(markup, base_url)
    return article if len(article.body) >= MIN_BODY_CHARS else None


def _usable(row: sqlite3.Row) -> bool:
    return row["body"] is not None and len(row["body"]) >= MIN_BODY_CHARS


def _is_fresh(row: sqlite3.Row | None, now: datetime) -> bool:
    """True when the stored read need not be repeated: a good read is kept for CACHE_DAYS, a failed one for RETRY_DAYS."""
    if row is None:
        return False
    age = now - db.from_iso(row["fetched_at"])
    if not _usable(row):
        return row["body"] is None and age < timedelta(days=RETRY_DAYS)
    return age < timedelta(days=CACHE_DAYS)


def _rows(conn: sqlite3.Connection, item_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not item_ids:
        return {}
    marks = ", ".join("?" for _ in item_ids)
    return {row["item_id"]: row for row in conn.execute(f"SELECT * FROM personal_articles WHERE item_id IN ({marks})", item_ids)}


def cached(conn: sqlite3.Connection, item_ids: list[int], now: datetime) -> dict[int, ArticleText]:
    """The usable article texts stored and still fresh."""
    return {
        item_id: ArticleText(body=row["body"], links=tuple(_json_list(row["links_json"])))
        for item_id, row in _rows(conn, item_ids).items()
        if _usable(row) and _is_fresh(row, now)
    }


def ensure(conn: sqlite3.Connection, items: list[dict], *, now: datetime, user_agent: str, limit: int = 150,
           delay_seconds: float = SAME_HOST_DELAY_SECONDS) -> dict[int, ArticleText]:
    """Reads the articles that are not fresh yet (up to `limit`), stores them, and returns every usable text now."""
    rows = _rows(conn, [item["id"] for item in items])
    todo = [item for item in items if not _is_fresh(rows.get(item["id"]), now) and item.get("url")][:limit]
    if todo:
        last_visit: dict[str, float] = {}
        with httpx.Client(timeout=12.0, follow_redirects=True,
                          headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}) as client:
            for item in todo:
                host = text.host_of(item["url"])
                wait = delay_seconds - (time.monotonic() - last_visit.get(host, -delay_seconds))
                if wait > 0:
                    time.sleep(wait)
                last_visit[host] = time.monotonic()
                _store(conn, item["id"], fetch(client, item["url"]), now)
        log.info("Read %d article(s) for the client brief", len(todo))
    return cached(conn, [item["id"] for item in items], now)


def _store(conn: sqlite3.Connection, item_id: int, article: ArticleText | None, now: datetime) -> None:
    import json

    with conn:
        conn.execute(
            """INSERT INTO personal_articles (item_id, fetched_at, body, links_json) VALUES (?, ?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET fetched_at = excluded.fetched_at, body = excluded.body,
                   links_json = excluded.links_json""",
            (item_id, db.to_iso(now), article.body if article else None,
             json.dumps(list(article.links)) if article else "[]"),
        )



def _json_list(value: str | None) -> list[str]:
    import json

    try:
        return list(json.loads(value or "[]"))
    except ValueError:
        return []
