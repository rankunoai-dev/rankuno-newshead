"""Turns raw feed entries and API results into stored items, according to the source type.

Every item records where a story really comes from:
  url             the original article, or the discussion itself for text posts (Reddit, Ask HN)
  discovered_via  the aggregator page worth linking to, when there is one (Reddit/HN thread, Techmeme)
  publisher       the original publisher's name, when the aggregator tells us (Google News, Techmeme)
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from . import db, text
from .config import Source

HACKERNEWS_ITEM_URL = "https://news.ycombinator.com/item?id={}"


def entry_to_item(
    entry, source: Source, now: datetime, max_age: timedelta, excluded_publishers: Iterable[str] = ()
) -> dict | None:
    """A feedparser entry as an item dict, or None when it is unusable, too old or excluded."""
    link = (entry.get("link") or "").strip()
    title = text.clean_title(text.html_to_text(entry.get("title", "")))
    if not link.startswith(("http://", "https://")) or not title:
        return None

    published = min(_entry_time(entry) or now, now)  # some feeds post-date entries
    if now - published > max_age:
        return None

    content_html = entry["content"][0].get("value", "") if entry.get("content") else ""
    summary_html = entry.get("summary", "")
    markup = content_html or summary_html

    url, discovered_via, publisher = _resolve_origin(entry, link, markup, source)
    if text.publisher_matches(publisher, url, excluded_publishers):
        return None
    if source.type == "google_news":
        title = text.strip_publisher_suffix(title, publisher)

    # Google News and Techmeme summaries only repeat the headline; enrichment fills these from the article page.
    excerpt = "" if source.type in ("google_news", "techmeme") else _excerpt(summary_html, content_html)

    return _item(
        source,
        url=url,
        discovered_via=discovered_via,
        publisher=publisher,
        title=title,
        author=entry.get("author"),
        published=published,
        excerpt=excerpt,
        image_url=None if source.type == "google_news" else _entry_image(entry, markup),
    )


def hackernews_hit_to_item(hit: dict, source: Source, now: datetime, max_age: timedelta) -> dict | None:
    """An Algolia Hacker News search hit as an item dict."""
    title = text.clean_title(text.html_to_text(hit.get("title") or ""))
    created = hit.get("created_at_i")
    if not title or not hit.get("objectID") or not created:
        return None
    published = min(datetime.fromtimestamp(created, tz=timezone.utc), now)
    if now - published > max_age:
        return None

    thread = HACKERNEWS_ITEM_URL.format(hit["objectID"])
    article = hit.get("url")
    return _item(
        source,
        url=article or thread,
        discovered_via=thread if article else None,
        publisher=None,
        title=title,
        author=hit.get("author"),
        published=published,
        excerpt=text.make_excerpt(text.html_to_text(hit.get("story_text") or "")),
        image_url=None,
    )


def _resolve_origin(entry, link: str, markup: str, source: Source) -> tuple[str, str | None, str | None]:
    if source.type == "reddit":
        external = text.reddit_external_link(markup)
        return (external, link, None) if external else (link, None, None)
    if source.type == "techmeme":
        article = text.first_external_link(markup, own_domain="techmeme.com")
        if not article:
            return link, None, "Techmeme"
        publisher = text.techmeme_publisher(markup)
        if publisher and publisher.startswith("@") and text.host_matches(text.host_of(article), ("x.com", "twitter.com")):
            publisher = f"{publisher} on X"  # Techmeme also cites posts on X
        return article, link, publisher
    if source.type == "google_alerts":
        return text.unwrap_google_redirect(link), None, None
    if source.type == "google_news":
        # The link is an encoded Google redirect; it is resolved only for stories chosen for an issue.
        return link, None, (entry.get("source") or {}).get("title")
    return link, None, None


def _item(
    source: Source,
    *,
    url: str,
    discovered_via: str | None,
    publisher: str | None,
    title: str,
    author: str | None,
    published: datetime,
    excerpt: str,
    image_url: str | None,
) -> dict:
    return {
        "url_hash": text.url_key(url),
        "source_id": source.id,
        "title": title,
        "url": text.canonical_url(url),
        "discovered_via": discovered_via,
        "publisher": publisher,
        "author": author,
        "published_at": db.to_iso(published),
        "excerpt": excerpt,
        "image_url": image_url,
    }


def _excerpt(summary_html: str, content_html: str) -> str:
    excerpt = text.make_excerpt(text.html_to_text(summary_html))
    if len(excerpt) < 80 and content_html:
        from_content = text.make_excerpt(text.html_to_text(content_html))
        if len(from_content) > len(excerpt):
            excerpt = from_content
    return excerpt


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(calendar.timegm(value), tz=timezone.utc)
    return None


def _entry_image(entry, markup: str) -> str | None:
    for key in ("media_content", "media_thumbnail"):
        for media in entry.get(key) or []:
            url = media.get("url")
            is_image = media.get("medium", "image") == "image" and not media.get("type", "").startswith("video")
            if is_image and text.usable_image_url(url):
                return url
    for enclosure in entry.get("enclosures") or []:
        if enclosure.get("type", "").startswith("image/") and text.usable_image_url(enclosure.get("href")):
            return enclosure["href"]
    return text.first_image(markup)
