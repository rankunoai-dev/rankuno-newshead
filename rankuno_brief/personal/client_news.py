"""Per-client news searches. Each active client's name, aliases and competitors become one Google News search, run
with the daily fetch. Items are stored with a client-news- source id: the client feeds use them, and the general
brief never does."""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3

from .. import fetch
from ..config import Config, Source
from . import store

log = logging.getLogger(__name__)

PREFIX = "client-news-"
MAX_TERMS = 12
SECTION = "martech_analytics"  # any valid section id: the general brief never selects these items


def is_client_news(source_id: str) -> bool:
    return source_id.startswith(PREFIX)


def search_terms(client_name: str, aliases: list[str], competitors: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for term in [client_name, *aliases, *competitors]:
        cleaned = " ".join(str(term).split())
        if cleaned:
            seen.setdefault(cleaned.lower(), cleaned)
    return list(seen.values())[:MAX_TERMS]


def query_for(terms: list[str]) -> str:
    return "(" + " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms) + ")"


def sources_for(conn: sqlite3.Connection) -> list[Source]:
    sources = []
    for row in store.clients_of(conn):
        if row["status"] != "active":
            continue
        competitors = [entry["name"] for entry in json.loads(row["competitors_json"] or "[]")]
        terms = search_terms(row["client_name"], json.loads(row["aliases_json"] or "[]"), competitors)
        if not terms:
            continue
        sources.append(Source(
            id=f"{PREFIX}{row['id']}",
            name=f"Google News: {row['client_name']}",
            section=SECTION,
            type="google_news",
            query=query_for(terms),
            weight=1.0,
            personal_only=True,
        ))
    return sources


def fetch_client_news(cfg: Config, conn: sqlite3.Connection) -> int:
    """Runs the per-client searches once. Returns the number of new items."""
    sources = sources_for(conn)
    if not sources:
        return 0
    summary = fetch.run_fetch(dataclasses.replace(cfg, sources=tuple(sources)), conn)
    log.info("Client news: %d searches ok, %d failed, %d new items", summary.ok, summary.failed, summary.new_items)
    return summary.new_items
