"""Choosing each client's stories from the news already fetched. Pure functions: no database, no network.

Terms are tiered, and a headline counts more than a summary (summary weights are 40% of the headline's):
    client name or alias, competitor   5    the strongest signal
    core keyword                       4    strategic topics, e.g. "strategic sourcing", "direct sourcing"
    operational keyword                2    operating topics, e.g. "yard management", "freight visibility"
    broad keyword                      1    context words, e.g. "supply chain", "spend management";
                                            they count only when at least two broad words match, and the
                                            headline has one of them

A story is kept when it has a name, competitor, core or operational hit, or two broad hits; and its score reaches
the minimum. Any exclude keyword drops it.

Executive appointments ("appoints", "names", "hires", "steps down", ...) are kept only when the headline names the
client or a competitor, so an unrelated company's new chief officer does not reach a client's feed.

Each story is labelled with its content type, from the URL and wording: "editorial", "sponsored" or "vendor".
Short names (4 characters or fewer, e.g. "GEP") count in the headline, or in the summary only with broad context.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

SHORT_NAME = 4
TIER_WEIGHTS = {"name": 5.0, "competitor": 5.0, "core": 4.0, "operational": 2.0, "broad": 1.0}
TITLE_FACTOR = 1.0
BODY_FACTOR = 0.4
GENERIC_BROAD_HITS = 2
STRATEGIC_TIERS = ("name", "competitor", "core")

APPOINTMENT = re.compile(
    r"\b(appoints?|appointed|appointing|names?|named|hires?|hired|steps? down|joins?|promotes?|promoted)\b", re.IGNORECASE
)
SPONSORED = re.compile(
    r"/spons|/sponsored|sponsored (?:content|by|post|report|feature|article)|paid (?:post|content)|partner (?:content|post)"
    r"|advertorial",
    re.IGNORECASE,
)
VENDOR = re.compile(
    r"\b(e-?books?|white ?papers?|webinars?|in this (?:e-?book|report|guide|whitepaper)|vendor report|analyst report"
    r"|free download|download (?:the|our) (?:report|guide))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Terms:
    names: tuple[str, ...]  # client name and aliases
    competitors: tuple[str, ...]
    core: tuple[str, ...]
    operational: tuple[str, ...]
    broad: tuple[str, ...]  # broad keywords and the chosen industries' keywords
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class Match:
    item_id: int
    score: float
    reasons: tuple[str, ...]
    strategic: bool  # a client, competitor or core hit (not only operational or broad context)
    content_type: str  # editorial | sponsored | vendor


@lru_cache(maxsize=512)
def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _hits(terms: Iterable[str], text: str) -> list[str]:
    return [term for term in terms if term and _pattern(term).search(text)]


def content_type(item: Mapping) -> str:
    """Sponsored and vendor pieces are labelled, so the team can tell them from independent journalism."""
    url = item["url"] or ""
    title = item["title"] or ""
    excerpt = item["excerpt"] or ""
    if SPONSORED.search(f"{url} {title} {excerpt}"):
        return "sponsored"
    if VENDOR.search(f"{title} {excerpt}"):
        return "vendor"
    return "editorial"


def score_item(item: Mapping, terms: Terms, min_score: float) -> Match | None:
    title = item["title"] or ""
    body = " ".join(part for part in (item["excerpt"] or "", item["publisher"] or "") if part)
    if _hits(terms.excludes, title) or _hits(terms.excludes, body):
        return None

    # An executive move is kept only when the headline names the client or a competitor.
    if APPOINTMENT.search(title) and not (_hits(terms.names, title) or _hits(terms.competitors, title)):
        return None

    groups = {
        "name": terms.names, "competitor": terms.competitors, "core": terms.core,
        "operational": terms.operational, "broad": terms.broad,
    }
    # tier -> (terms hit in the headline, terms hit in the summary only)
    found: dict[str, tuple[list[str], list[str]]] = {}
    for tier, group in groups.items():
        in_title = _hits(group, title)
        in_body = [term for term in _hits(group, body) if term not in in_title]
        found[tier] = (in_title, in_body)

    broad_count = len({term.lower() for pair in (found["broad"][0], found["broad"][1]) for term in pair})
    # A short name ("GEP") counts in the headline, or in the summary only alongside broad context.
    names_title, names_body = found["name"]
    found["name"] = (names_title, [term for term in names_body if len(term) > SHORT_NAME or broad_count > 0])

    strategic = any(found[tier][0] or found[tier][1] for tier in STRATEGIC_TIERS)
    operational = bool(found["operational"][0] or found["operational"][1])
    generic = broad_count >= GENERIC_BROAD_HITS
    if not (strategic or operational or generic):
        return None

    score = 0.0
    reasons: list[str] = []
    for tier in ("name", "competitor", "core", "operational", "broad"):
        if tier == "broad" and not generic:
            continue  # one broad word is not enough, and broad words add nothing to a story that has no other reason
        in_title, in_body = found[tier]
        for term in in_title:
            score += TIER_WEIGHTS[tier] * TITLE_FACTOR
            reasons.append(term)
        for term in in_body:
            score += TIER_WEIGHTS[tier] * BODY_FACTOR
            reasons.append(term)
    if score < min_score:
        return None
    return Match(
        item_id=item["id"],
        score=round(score, 2),
        reasons=tuple(dict.fromkeys(reasons)),
        strategic=strategic,
        content_type=content_type(item),
    )


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
