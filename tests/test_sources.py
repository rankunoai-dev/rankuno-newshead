"""Phase 2 source types: Google News, Google Alerts, Techmeme, Hacker News, plus link resolution and enrichment."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import feedparser
import httpx
import pytest
from conftest import NOW

from rankuno_brief import adapters, compose, db, enrich, google_news
from rankuno_brief.config import ConfigError, Source, _validate

MAX_AGE = timedelta(days=14)

GOOGLE_NEWS_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title>
<item>
  <title>Google expands AI Mode to 40 more countries - Reuters</title>
  <link>https://news.google.com/rss/articles/CBMiAbC123?oc=5</link>
  <pubDate>Sat, 12 Sep 2026 08:00:00 GMT</pubDate>
  <description>&lt;a href="https://news.google.com/rss/articles/CBMiAbC123"&gt;Google expands AI Mode&lt;/a&gt;&amp;nbsp;&lt;font color="#6f6f6f"&gt;Reuters&lt;/font&gt;</description>
  <source url="https://www.reuters.com">Reuters</source>
</item>
<item>
  <title>Acme launches AI SEO platform - EIN Presswire</title>
  <link>https://news.google.com/rss/articles/CBMiPressRelease?oc=5</link>
  <pubDate>Sat, 12 Sep 2026 09:00:00 GMT</pubDate>
  <source url="https://www.einpresswire.com">EIN Presswire</source>
</item>
</channel></rss>"""

TECHMEME_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Techmeme</title>
<item>
  <title>OpenAI rolls out ChatGPT ads to Europe</title>
  <link>https://www.techmeme.com/260915/p2#a260915p2</link>
  <pubDate>Mon, 13 Sep 2026 08:00:00 GMT</pubDate>
  <description>&lt;a href="https://www.theverge.com/ai/openai-ads-europe"&gt;&lt;img src="http://www.techmeme.com/260915/i2.jpg" /&gt;&lt;/a&gt;
  &lt;p&gt;&lt;a href="https://www.techmeme.com/260915/p2#a260915p2" title="Techmeme permalink"&gt;&lt;img src="http://www.techmeme.com/img/pml.png" /&gt;&lt;/a&gt;
  Emma Roth / &lt;a href="https://www.theverge.com/"&gt;The Verge&lt;/a&gt;:&lt;br /&gt;&lt;b&gt;OpenAI rolls out ChatGPT ads to Europe&lt;/b&gt;&lt;/p&gt;</description>
</item>
</channel></rss>"""

GOOGLE_ALERTS_ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Google Alert - quora AI Overviews</title>
<entry>
  <title type="html">Why did &lt;b&gt;AI Overviews&lt;/b&gt; stop citing my site? - Quora</title>
  <link href="https://www.google.com/url?rct=j&amp;sa=t&amp;url=https://www.quora.com/Why-did-AI-Overviews-stop-citing-my-site&amp;ct=ga&amp;cd=abc"/>
  <published>2026-09-12T08:00:00Z</published>
  <content type="html">Answers about &lt;b&gt;AI Overviews&lt;/b&gt; citations.</content>
</entry>
</feed>"""


def test_google_news_entry_strips_suffix_keeps_publisher_and_drops_press_releases(cfg):
    source = cfg.source_map["google-news-ai-search"]
    entries = feedparser.parse(GOOGLE_NEWS_RSS).entries
    items = [adapters.entry_to_item(entry, source, NOW, MAX_AGE, cfg.exclude_publishers) for entry in entries]

    story, press_release = items
    assert story["title"] == "Google expands AI Mode to 40 more countries"
    assert story["publisher"] == "Reuters"
    assert story["url"].startswith("https://news.google.com/rss/articles/CBMiAbC123")
    assert story["excerpt"] == "" and story["image_url"] is None  # filled in later from the article page
    assert press_release is None


def test_techmeme_entry_points_at_original_article(cfg):
    source = cfg.source_map["techmeme"]
    entry = feedparser.parse(TECHMEME_RSS).entries[0]
    item = adapters.entry_to_item(entry, source, NOW, MAX_AGE)
    assert item["url"] == "https://www.theverge.com/ai/openai-ads-europe"
    assert item["discovered_via"] == "https://www.techmeme.com/260915/p2#a260915p2"
    assert item["publisher"] == "The Verge"


def test_techmeme_entry_citing_a_post_on_x(cfg):
    rss = TECHMEME_RSS.replace("https://www.theverge.com/ai/openai-ads-europe", "https://x.com/dkokotajlo/status/1") \
                      .replace("Emma Roth / &lt;a href=\"https://www.theverge.com/\"&gt;The Verge&lt;/a&gt;",
                               "&lt;a href=\"https://x.com/dkokotajlo\"&gt;@dkokotajlo&lt;/a&gt;")
    item = adapters.entry_to_item(feedparser.parse(rss).entries[0], cfg.source_map["techmeme"], NOW, MAX_AGE)
    assert (item["url"], item["publisher"]) == ("https://x.com/dkokotajlo/status/1", "@dkokotajlo on X")


def test_google_alerts_entry_unwraps_redirect(cfg):
    source = replace(cfg.source_map["quora-ai-search"], enabled=True)
    entry = feedparser.parse(GOOGLE_ALERTS_ATOM).entries[0]
    item = adapters.entry_to_item(entry, source, NOW, MAX_AGE)
    assert item["url"] == "https://www.quora.com/Why-did-AI-Overviews-stop-citing-my-site"
    assert item["title"] == "Why did AI Overviews stop citing my site? - Quora"
    assert item["excerpt"] == "Answers about AI Overviews citations."


def test_hackernews_hits_become_link_stories_or_discussions(cfg):
    source = cfg.source_map["hacker-news"]
    created = int((NOW - timedelta(hours=5)).timestamp())
    link_hit = {"objectID": "101", "title": "Perplexity launches ads", "url": "https://perplexity.ai/x", "created_at_i": created}
    ask_hit = {"objectID": "102", "title": "Ask HN: Is SEO dead?", "story_text": "<p>Traffic is down.</p>", "created_at_i": created}

    link_item = adapters.hackernews_hit_to_item(link_hit, source, NOW, MAX_AGE)
    ask_item = adapters.hackernews_hit_to_item(ask_hit, source, NOW, MAX_AGE)
    assert (link_item["url"], link_item["discovered_via"]) == ("https://perplexity.ai/x", "https://news.ycombinator.com/item?id=101")
    assert (ask_item["url"], ask_item["discovered_via"]) == ("https://news.ycombinator.com/item?id=102", None)
    assert ask_item["excerpt"] == "Traffic is down."


def test_google_news_feed_url_includes_query_and_time_window(cfg):
    url = cfg.source_map["google-news-seo"].feed_url
    query = parse_qs(urlsplit(url).query)["q"][0]
    assert query.startswith('"core update" OR') and query.endswith("when:2d")


def test_config_rejects_google_news_without_query_and_unknown_types(cfg):
    broken = replace(cfg, sources=(Source(id="a", name="A", section="seo", type="google_news"),
                                   Source(id="b", name="B", section="seo", type="twitter", url="https://x.com")))
    with pytest.raises(ConfigError) as error:
        _validate(broken)
    assert "needs a query" in str(error.value) and "unknown type 'twitter'" in str(error.value)


def _google_news_transport(target_url: str, sg: str = "SIG", ts: str = "1757750000"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=f'<c-wiz><div jscontroller="x" data-n-a-sg="{sg}" data-n-a-ts="{ts}"></div></c-wiz>')
        body = parse_qs(request.content.decode())["f.req"][0]
        assert "CBMiAbC123" in body and sg in body and ts in body
        inner = json.dumps(["garturlres", target_url, 1])
        payload = json.dumps([["wrb.fr", "Fbv4je", inner, None, None, None, "generic"], ["di", 10], ["af.httprm", 9, "x", 1]])
        return httpx.Response(200, text=f")]}}'\n\n{payload}")

    return httpx.MockTransport(handler)


def test_google_news_decode_resolves_publisher_url():
    client = httpx.Client(transport=_google_news_transport("https://www.reuters.com/tech/ai-mode-expands"))
    assert google_news.decode(client, "https://news.google.com/rss/articles/CBMiAbC123?oc=5") == "https://www.reuters.com/tech/ai-mode-expands"


def test_google_news_decode_returns_none_when_page_has_no_signature():
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>consent</html>")))
    assert google_news.decode(client, "https://news.google.com/rss/articles/CBMiAbC123") is None


def test_resolve_links_updates_story_and_also_reported_by(monkeypatch):
    targets = {"https://news.google.com/rss/articles/main": "https://www.reuters.com/a",
               "https://news.google.com/rss/articles/other": "https://www.bloomberg.com/b"}
    monkeypatch.setattr(google_news, "decode", lambda client, url: targets.get(url))
    story = compose.Story(item_id=7, title="t", url="https://news.google.com/rss/articles/main", source_id="s",
                          source_name="Reuters", published_at=NOW, excerpt="", image_url=None, section_id="seo", score=1,
                          also_reported_by=[("Bloomberg", "https://news.google.com/rss/articles/other"),
                                            ("The Verge", "https://www.theverge.com/c"),
                                            ("Unknown", "https://news.google.com/rss/articles/unresolvable")])
    resolved = google_news.resolve_links([story], sleep=lambda _: None)
    assert resolved == {7: "https://www.reuters.com/a"}
    assert story.also_reported_by == [("Bloomberg", "https://www.bloomberg.com/b"),
                                      ("The Verge", "https://www.theverge.com/c"),
                                      ("Unknown", "https://news.google.com/rss/articles/unresolvable")]


def test_parse_head_reads_image_and_description_with_apostrophes():
    head = """<head>
      <meta content="https://cdn.example.com/hero.jpg" property="og:image">
      <meta property="og:description" content="Google's AI Mode now reaches 40 countries &amp; more.">
      <meta name="description" content="Fallback description">
    </head>"""
    meta = enrich.parse_head(head, "https://example.com/story")
    assert meta.image_url == "https://cdn.example.com/hero.jpg"
    assert meta.description == "Google's AI Mode now reaches 40 countries & more."


def test_old_database_gains_publisher_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, url_hash TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL, "
                "title TEXT NOT NULL, url TEXT NOT NULL, discovered_via TEXT, author TEXT, published_at TEXT NOT NULL, "
                "fetched_at TEXT NOT NULL, excerpt TEXT NOT NULL DEFAULT '', image_url TEXT)")
    old.execute("INSERT INTO items (url_hash, source_id, title, url, published_at, fetched_at) VALUES ('h', 's', 't', 'u', 'p', 'f')")
    old.commit()
    old.close()

    conn = db.connect(path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    assert "publisher" in columns
    assert conn.execute("SELECT title FROM items").fetchone()["title"] == "t"
    conn.close()
