"""Rule-based relevance: which section a story belongs to and how important it is. No LLM involved."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Source, Taxonomy

TITLE_MULTIPLIER = 3.0
EXTRA_MATCH_FACTOR = 0.5  # 2nd keyword counts half, 3rd a quarter...: one strong match beats many weak ones
RELEVANCE_CAP = 15.0  # stops keyword-stuffed posts from outranking everything
DAILY_DECAY = 0.08
MIN_RECENCY = 0.6


@dataclass(frozen=True)
class ScoreResult:
    section_id: str
    score: float
    matched_keywords: tuple[str, ...]


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    # "post-AI Mode" matches AI Mode, but "GEO" does not match inside "geo-targeting".
    return re.compile(r"(?<!\w)" + re.escape(keyword) + r"(?![\w-])", re.IGNORECASE)


def _blank_out(pattern: re.Pattern[str], value: str) -> str:
    return pattern.sub(lambda match: " " * len(match.group()), value)


class Scorer:
    def __init__(self, taxonomy: Taxonomy) -> None:
        # Longest keywords first, so "Google Analytics" is consumed before "analytics" can match inside it.
        self._sections = [
            (
                section,
                [
                    (keyword, weight, _keyword_pattern(keyword))
                    for keyword, weight in sorted(section.keywords.items(), key=lambda pair: -len(pair[0]))
                ],
            )
            for section in taxonomy.sections
        ]
        self._excluded = [_keyword_pattern(keyword) for keyword in taxonomy.exclude_title_keywords]
        self._evergreen = [re.compile(pattern, re.IGNORECASE) for pattern in taxonomy.evergreen_title_patterns]
        self._evergreen_multiplier = taxonomy.evergreen_multiplier

    def score(self, title: str, excerpt: str, source: Source, age_days: float) -> ScoreResult | None:
        """Score one story, or return None when it should be left out entirely."""
        if any(pattern.search(title) for pattern in self._excluded):
            return None

        best_section, best_relevance, best_matched = source.section, 0.0, ()
        for section, keywords in self._sections:
            contributions, matched = [], []
            title_left, excerpt_left = title, excerpt
            for keyword, weight, pattern in keywords:
                if pattern.search(title_left):
                    contributions.append(weight * TITLE_MULTIPLIER)
                elif pattern.search(excerpt_left):
                    contributions.append(weight)
                else:
                    continue
                matched.append(keyword)
                title_left, excerpt_left = _blank_out(pattern, title_left), _blank_out(pattern, excerpt_left)
            contributions.sort(reverse=True)
            relevance = section.weight * sum(value * EXTRA_MATCH_FACTOR**rank for rank, value in enumerate(contributions))
            if relevance > best_relevance:
                best_section, best_relevance, best_matched = section.id, relevance, tuple(matched)

        if best_relevance == 0 and source.require_keywords:
            return None

        section_id = source.section if source.community else best_section
        recency = max(MIN_RECENCY, 1.0 - DAILY_DECAY * max(age_days, 0.0))
        score = source.weight * (1.0 + min(best_relevance, RELEVANCE_CAP)) * recency
        if any(pattern.search(title) for pattern in self._evergreen):
            score *= self._evergreen_multiplier
        return ScoreResult(section_id=section_id, score=round(score, 3), matched_keywords=best_matched)
