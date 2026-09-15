from dataclasses import replace
from datetime import timedelta

import feedparser
import httpx
from conftest import NOW

from rankuno_brief import db
from rankuno_brief.adapters import entry_to_item
from rankuno_brief.fetch import SourceOutcome, fetch_source, get_with_retries

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel><title>Test</title>
  <item>
    <title>Google expands AI Mode via @sejournal, @writer</title>
    <link>https://example.com/ai-mode?utm_source=rss</link>
    <pubDate>Sat, 12 Sep 2026 08:00:00 GMT</pubDate>
    <description>&lt;p&gt;Google expanded AI Mode to more markets.&lt;/p&gt;</description>
    <media:content url="https://example.com/ai-mode.jpg" medium="image"/>
  </item>
  <item>
    <title>Ancient news</title>
    <link>https://example.com/old</link>
    <pubDate>Mon, 01 Jun 2026 08:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Scheduled for tomorrow</title>
    <link>https://example.com/future</link>
    <pubDate>Mon, 14 Sep 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


def test_entry_to_item_cleans_and_filters(cfg):
    source = cfg.source_map["search-engine-land"]
    entries = feedparser.parse(RSS).entries
    items = [entry_to_item(entry, source, NOW, timedelta(days=14)) for entry in entries]

    fresh, ancient, future = items
    assert fresh["title"] == "Google expands AI Mode"
    assert fresh["url"] == "https://example.com/ai-mode"
    assert fresh["excerpt"] == "Google expanded AI Mode to more markets."
    assert fresh["image_url"] == "https://example.com/ai-mode.jpg"
    assert ancient is None
    assert future["published_at"] == db.to_iso(NOW)  # post-dated entries are clamped to now


def test_get_with_retries_recovers_from_rate_limit():
    responses = iter([httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(503), httpx.Response(200, text="ok")])
    client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
    delays = []
    response = get_with_retries(client, "https://example.com/feed", {}, retries=3, sleep=delays.append)
    assert response.status_code == 200
    assert delays[0] == 1.0
    assert len(delays) == 2


def test_get_with_retries_gives_up_after_limit():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    response = get_with_retries(client, "https://example.com/feed", {}, retries=2, sleep=lambda _: None)
    assert response.status_code == 500


def test_fetch_source_reports_errors_instead_of_raising(cfg):
    def explode(request):
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(explode))
    no_retry = replace(cfg, fetch=replace(cfg.fetch, retries=0))
    outcome = fetch_source(client, cfg.source_map["search-engine-land"], no_retry, (None, None), NOW)
    assert outcome.status == "error"
    assert "ConnectError" in outcome.error


def test_fetch_source_uses_conditional_get(cfg):
    seen_headers = {}

    def not_modified(request):
        seen_headers.update(request.headers)
        return httpx.Response(304)

    client = httpx.Client(transport=httpx.MockTransport(not_modified))
    outcome = fetch_source(client, cfg.source_map["search-engine-land"], cfg, ('"abc"', "Sat, 12 Sep 2026 08:00:00 GMT"), NOW)
    assert outcome.status == "not_modified"
    assert seen_headers["if-none-match"] == '"abc"'


def test_record_outcome_deduplicates_and_tracks_health(cfg, conn):
    source = cfg.source_map["search-engine-land"]
    db.sync_sources(conn, [source])
    run_id = db.start_fetch_run(conn, NOW)
    item = entry_to_item(feedparser.parse(RSS).entries[0], source, NOW, timedelta(days=14))

    first = db.record_outcome(conn, run_id, SourceOutcome(source=source, status="ok", items=[item]), NOW)
    again = db.record_outcome(conn, run_id, SourceOutcome(source=source, status="ok", items=[item]), NOW)
    assert (first, again) == (1, 0)

    db.record_outcome(conn, run_id, SourceOutcome(source=source, status="error", error="HTTP 500"), NOW)
    db.record_outcome(conn, run_id, SourceOutcome(source=source, status="error", error="HTTP 500"), NOW)
    health = {row["id"]: row for row in db.source_health(conn)}
    assert health[source.id]["consecutive_failures"] == 2

    db.record_outcome(conn, run_id, SourceOutcome(source=source, status="not_modified"), NOW)
    health = {row["id"]: row for row in db.source_health(conn)}
    assert health[source.id]["consecutive_failures"] == 0
