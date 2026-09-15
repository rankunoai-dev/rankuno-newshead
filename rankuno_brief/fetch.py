"""Daily ingestion: download every enabled source, turn its entries into items and store the new ones.

Sources on the same host are fetched one after another by a single worker, so we never hit a
site in parallel (Reddit and Google rate-limit that). A failing source is logged and skipped;
it never stops the rest of the run.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from . import adapters, db
from .config import Config, Source

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
HACKERNEWS_LOOKBACK = timedelta(days=3)


@dataclass
class SourceOutcome:
    source: Source
    status: str = "error"  # ok | not_modified | error
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    items: list[dict] = field(default_factory=list)
    entries_seen: int = 0
    error: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True)
class FetchSummary:
    ok: int
    failed: int
    new_items: int
    failures: tuple[SourceOutcome, ...]


def run_fetch(cfg: Config, conn: sqlite3.Connection) -> FetchSummary:
    now = datetime.now(timezone.utc)
    sources = [source for source in cfg.sources if source.enabled]
    db.sync_sources(conn, sources)
    validators = db.source_validators(conn)
    run_id = db.start_fetch_run(conn, now)

    by_host: dict[str, list[Source]] = defaultdict(list)
    for source in sources:
        by_host[source.host].append(source)

    outcomes: list[SourceOutcome] = []
    workers = max(1, min(cfg.fetch.max_workers, len(by_host)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for group in pool.map(lambda group: _fetch_group(group, cfg, validators, now), by_host.values()):
            outcomes.extend(group)

    new_items = 0
    for outcome in outcomes:
        count = db.record_outcome(conn, run_id, outcome, now)
        new_items += count
        if outcome.status != "error":
            log.info("%-28s %-12s %3d entries, %3d new", outcome.source.id, outcome.status, outcome.entries_seen, count)

    failures = tuple(outcome for outcome in outcomes if outcome.status == "error")
    db.finish_fetch_run(conn, run_id, datetime.now(timezone.utc), len(outcomes) - len(failures), len(failures), new_items)
    return FetchSummary(ok=len(outcomes) - len(failures), failed=len(failures), new_items=new_items, failures=failures)


def _fetch_group(group: list[Source], cfg: Config, validators: dict, now: datetime) -> list[SourceOutcome]:
    outcomes = []
    headers = {"User-Agent": cfg.fetch.user_agent}
    with httpx.Client(timeout=cfg.fetch.timeout_seconds, follow_redirects=True, headers=headers) as client:
        for index, source in enumerate(group):
            if index:
                time.sleep(cfg.fetch.same_host_delay_seconds)
            outcomes.append(fetch_source(client, source, cfg, validators.get(source.id, (None, None)), now))
    return outcomes


def fetch_source(
    client: httpx.Client, source: Source, cfg: Config, validator: tuple[str | None, str | None], now: datetime
) -> SourceOutcome:
    started = time.monotonic()
    outcome = SourceOutcome(source=source)
    try:
        if source.type == "hackernews":
            _fetch_hackernews(client, source, cfg, now, outcome)
        else:
            _fetch_feed(client, source, cfg, validator, now, outcome)
    except Exception as exc:  # noqa: BLE001 - one broken source must never stop the run
        outcome.status = "error"
        outcome.error = f"{type(exc).__name__}: {exc}"

    if outcome.error:
        outcome.error = outcome.error[:500]
        log.warning("%-28s FAILED       %s", source.id, outcome.error)
    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    return outcome


def _fetch_feed(
    client: httpx.Client,
    source: Source,
    cfg: Config,
    validator: tuple[str | None, str | None],
    now: datetime,
    outcome: SourceOutcome,
) -> None:
    etag, last_modified = validator
    headers = {"Accept": FEED_ACCEPT}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    response = get_with_retries(client, source.feed_url, headers, cfg.fetch.retries)
    outcome.http_status = response.status_code
    if response.status_code == 304:
        outcome.status = "not_modified"
        return
    if response.status_code >= 400:
        outcome.error = f"HTTP {response.status_code}"
        return

    parsed = feedparser.parse(
        response.content,
        response_headers={"content-location": str(response.url), "content-type": response.headers.get("content-type", "")},
    )
    if parsed.bozo and not parsed.entries:
        outcome.error = f"Could not parse feed: {parsed.get('bozo_exception')!r}"
        return

    max_age = timedelta(days=cfg.fetch.max_item_age_days)
    outcome.status = "ok"
    outcome.etag = response.headers.get("etag")
    outcome.last_modified = response.headers.get("last-modified")
    outcome.entries_seen = len(parsed.entries)
    outcome.items = [
        item
        for entry in parsed.entries
        if (item := adapters.entry_to_item(entry, source, now, max_age, cfg.exclude_publishers)) is not None
    ]


def _fetch_hackernews(client: httpx.Client, source: Source, cfg: Config, now: datetime, outcome: SourceOutcome) -> None:
    since = int((now - HACKERNEWS_LOOKBACK).timestamp())
    params = httpx.QueryParams(
        {"tags": "story", "numericFilters": f"created_at_i>{since},points>={source.min_points}", "hitsPerPage": "200"}
    )
    response = get_with_retries(client, f"{source.feed_url}?{params}", {"Accept": "application/json"}, cfg.fetch.retries)
    outcome.http_status = response.status_code
    if response.status_code >= 400:
        outcome.error = f"HTTP {response.status_code}"
        return

    hits = response.json().get("hits") or []
    max_age = timedelta(days=cfg.fetch.max_item_age_days)
    outcome.status = "ok"
    outcome.entries_seen = len(hits)
    outcome.items = [item for hit in hits if (item := adapters.hackernews_hit_to_item(hit, source, now, max_age)) is not None]


def get_with_retries(
    client: httpx.Client, url: str, headers: dict[str, str], retries: int, sleep=time.sleep
) -> httpx.Response:
    """GET with exponential backoff on network errors, 429 and 5xx. Honours Retry-After."""
    attempt = 0
    while True:
        try:
            response = client.get(url, headers=headers)
        except httpx.TransportError as exc:
            if attempt >= retries:
                raise
            delay = _backoff(attempt)
            reason = type(exc).__name__
        else:
            if response.status_code not in RETRYABLE_STATUS or attempt >= retries:
                return response
            delay = _retry_after(response) or _backoff(attempt)
            reason = f"HTTP {response.status_code}"
        attempt += 1
        log.info("Retrying %s after %s in %.1fs (attempt %d of %d)", url, reason, delay, attempt, retries)
        sleep(delay)


def _backoff(attempt: int) -> float:
    return min(30.0, 2.0 ** (attempt + 1)) + random.uniform(0, 1)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))
