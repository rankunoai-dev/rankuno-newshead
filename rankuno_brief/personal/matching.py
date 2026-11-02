"""Choosing each client's stories from the news already fetched. Pure functions: no database, no network.

Scoring (a term found in the headline counts more than one found only in the summary):
    client name or alias     5   (summary 2)
    competitor               4   (summary 1.5)
    include keyword          3   (summary 1)
    industry keyword         1.5 (summary 0.5); counts only toward the generic-industry rule below

A story is kept when it has a client, competitor or keyword hit, or when at least two industry keywords match
(generic industry news). Any exclude keyword drops it. Short names (4 characters or fewer, e.g. "GEP") only count
when the headline has them or an industry keyword appears too, so an unrelated use of the word does not match.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

SHORT_NAME = 4
TITLE_WEIGHTS = {"name": 5.0, "competitor": 4.0, "keyword": 3.0, "industry": 1.5}
BODY_WEIGHTS = {"name": 2.0, "competitor": 1.5, "keyword": 1.0, "industry": 0.5}
GENERIC_INDUSTRY_HITS = 2


@dataclass(frozen=True)
class Terms:
    names: tuple[str, ...]  # client name and aliases
    competitors: tuple[str, ...]
    keywords: tuple[str, ...]
    excludes: tuple[str, ...]
    industry: tuple[str, ...]  # keywords of the chosen industries


@dataclass(frozen=True)
class Match:
    item_id: int
    score: float
    reasons: tuple[str, ...]


@lru_cache(maxsize=256)
def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _hits(terms: Iterable[str], text: str) -> list[str]:
    return [term for term in terms if term and _pattern(term).search(text)]


def score_item(item: Mapping, terms: Terms, min_score: float) -> Match | None:
    title = item["title"] or ""
    body = " ".join(part for part in (item["excerpt"] or "", item["publisher"] or "") if part)
    if _hits(terms.excludes, title) or _hits(terms.excludes, body):
        return None

    # Each category: (terms hit in headline, terms hit in summary only)
    found: dict[str, tuple[list[str], list[str]]] = {}
    for category, group in (
        ("name", terms.names), ("competitor", terms.competitors), ("keyword", terms.keywords), ("industry", terms.industry),
    ):
        in_title = _hits(group, title)
        in_body = [term for term in _hits(group, body) if term not in in_title]
        found[category] = (in_title, in_body)

    industry_count = len(set(found["industry"][0]) | set(found["industry"][1]))
    # A short name ("GEP") counts in the headline, or in the summary only alongside industry context.
    names_title, names_body = found["name"]
    names_body = [term for term in names_body if len(term) > SHORT_NAME or industry_count > 0]
    found["name"] = (names_title, names_body)

    strong = any(found[category][0] or found[category][1] for category in ("name", "competitor", "keyword"))
    generic = industry_count >= GENERIC_INDUSTRY_HITS
    if not (strong or generic):
        return None

    score = 0.0
    reasons: list[str] = []
    for category in ("name", "competitor", "keyword", "industry"):
        in_title, in_body = found[category]
        if category == "industry" and not generic:
            continue  # industry hits alone are not enough, and they do not add to a strong match either
        for term in in_title:
            score += TITLE_WEIGHTS[category]
            reasons.append(term)
        for term in in_body:
            score += BODY_WEIGHTS[category]
            reasons.append(term)
    if score < min_score:
        return None
    return Match(item_id=item["id"], score=round(score, 2), reasons=tuple(dict.fromkeys(reasons)))


def rank(
    items: Iterable[Mapping], terms: Terms, *, min_score: float, max_stories: int, max_per_source: int,
    exclude_ids: set[int] | None = None,
) -> list[Match]:
    """Best matches first (score, then newest), capped in total and per source. Items already featured are skipped."""
    skip = exclude_ids or set()
    scored: list[tuple[Match, Mapping]] = []
    for item in items:
        if item["id"] in skip:
            continue
        match = score_item(item, terms, min_score)
        if match is not None:
            scored.append((match, item))
    scored.sort(key=lambda pair: (-pair[0].score, -_timestamp(pair[1]["published_at"])))
    chosen: list[Match] = []
    per_source: dict[str, int] = {}
    for match, item in scored:
        if len(chosen) >= max_stories:
            break
        if per_source.get(item["source_id"], 0) >= max_per_source:
            continue
        per_source[item["source_id"]] = per_source.get(item["source_id"], 0) + 1
        chosen.append(match)
    return chosen


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0
