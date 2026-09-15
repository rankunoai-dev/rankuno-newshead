"""Turns stored items into issue content: score, merge duplicate stories, apply limits, group by section."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from . import db, text
from .config import Config, Source
from .scoring import Scorer

# Each additional publisher covering the same story raises its score by this fraction.
COVERAGE_BONUS = 0.2


@dataclass
class Story:
    item_id: int
    title: str
    url: str
    source_id: str
    source_name: str  # shown above the headline: the original publisher
    published_at: datetime
    excerpt: str
    image_url: str | None
    section_id: str
    score: float
    matched_keywords: tuple[str, ...] = ()
    via_name: str | None = None  # "Found via ..." when it reached us through an aggregator
    via_url: str | None = None  # link to the aggregator page (Reddit/HN thread, Techmeme), when useful
    is_discussion: bool = False
    also_reported_by: list[tuple[str, str]] = field(default_factory=list)  # (publisher name, article url)
    tokens: frozenset[str] = field(default=frozenset(), repr=False)


@dataclass
class IssueSection:
    id: str
    title: str
    stories: list[Story]


@dataclass
class IssueContent:
    top_stories: list[Story]
    sections: list[IssueSection]

    @property
    def stories(self) -> list[Story]:
        return self.top_stories + [story for section in self.sections for story in section.stories]

    @property
    def story_count(self) -> int:
        return len(self.stories)

    @property
    def source_count(self) -> int:
        return len({story.source_name for story in self.stories})


def build_content(rows: Iterable[Mapping], cfg: Config, now: datetime) -> IssueContent:
    scorer = Scorer(cfg.taxonomy)
    sources = cfg.source_map
    scored: list[Story] = []

    for row in rows:
        source = sources.get(row["source_id"])
        if source is None or not source.enabled:
            continue  # source was removed or disabled after the item was stored
        published_at = db.from_iso(row["published_at"])
        age_days = (now - published_at).total_seconds() / 86400
        result = scorer.score(row["title"], row["excerpt"], source, age_days)
        if result is None or result.score < cfg.issue.min_score:
            continue
        source_name, via_name = attribution(source, row)
        scored.append(
            Story(
                item_id=row["id"],
                title=row["title"],
                url=row["url"],
                source_id=source.id,
                source_name=source_name,
                published_at=published_at,
                excerpt=row["excerpt"],
                image_url=row["image_url"],
                section_id=result.section_id,
                score=result.score,
                matched_keywords=result.matched_keywords,
                via_name=via_name,
                via_url=row["discovered_via"] if via_name else None,
                is_discussion=text.is_discussion_url(row["url"]),
                tokens=text.title_tokens(row["title"]),
            )
        )

    scored.sort(key=lambda story: story.score, reverse=True)
    merged = [story for story in _merge_duplicates(scored, cfg) if _has_enough_coverage(story, cfg)]
    for story in merged:
        story.score = round(story.score * (1 + COVERAGE_BONUS * len(story.also_reported_by)), 3)
    merged.sort(key=lambda story: story.score, reverse=True)
    top, rest = _select(merged, cfg)

    sections = []
    for section in cfg.taxonomy.sections:
        stories = [story for story in rest if story.section_id == section.id]
        if stories:
            sections.append(IssueSection(id=section.id, title=section.title, stories=stories))
    return IssueContent(top_stories=top, sections=sections)


def attribution(source: Source, row: Mapping) -> tuple[str, str | None]:
    """(publisher shown above the headline, "Found via" label or None)."""
    if not source.aggregator:
        return source.name, None
    via = source.found_via
    if source.type == "reddit":
        via = text.reddit_community_name(row["discovered_via"] or row["url"]) or via
    if source.type in ("reddit", "hackernews") and not row["discovered_via"]:
        return via, None  # a text post: the discussion itself is the story
    origin = row["publisher"] or text.site_name(row["url"])
    return origin, (None if origin == via else via)


def drop_duplicate_urls(content: IssueContent) -> int:
    """Remove stories pointing at an article already in the issue (possible once Google News links are resolved)."""
    seen: dict[str, Story] = {}
    removed = 0

    def keep(story: Story) -> bool:
        nonlocal removed
        key = text.url_key(story.url)
        first = seen.setdefault(key, story)
        if first is story:
            return True
        removed += 1
        return False

    content.top_stories = [story for story in content.top_stories if keep(story)]
    for section in content.sections:
        section.stories = [story for story in section.stories if keep(story)]
    content.sections = [section for section in content.sections if section.stories]
    return removed


def _merge_duplicates(stories: list[Story], cfg: Config) -> list[Story]:
    """Group versions of the same story; lead with the best-scoring trusted version, list the others under it.

    `stories` must be sorted by score, highest first.
    """
    clusters: list[list[Story]] = []
    for story in stories:
        cluster = next((group for group in clusters if text.same_story(group[0].tokens, story.tokens)), None)
        if cluster is None:
            clusters.append([story])
        else:
            cluster.append(story)

    kept: list[Story] = []
    for cluster in clusters:
        lead = cluster[0]
        if not _is_trusted(lead, cfg):
            lead = next((story for story in cluster if _is_trusted(story, cfg)), lead)
        lead.score = cluster[0].score
        listed = {lead.source_name}
        lead.also_reported_by = []
        for story in cluster:
            if story is not lead and story.source_name not in listed:
                lead.also_reported_by.append((story.source_name, story.url))
                listed.add(story.source_name)
        kept.append(lead)
    return kept


def _is_trusted(story: Story, cfg: Config) -> bool:
    """A publisher's own feed we chose to follow, or a publisher on the trusted list."""
    return not cfg.source_map[story.source_id].aggregator or text.publisher_matches(
        story.source_name, story.url, cfg.trusted_publishers
    )


def _has_enough_coverage(story: Story, cfg: Config) -> bool:
    """Broad searches (Google News) surface content farms; require a trusted publisher or wider coverage."""
    required = cfg.source_map[story.source_id].min_coverage
    if required <= 1 or _is_trusted(story, cfg):
        return True
    return 1 + len(story.also_reported_by) >= required


def _select(stories: list[Story], cfg: Config) -> tuple[list[Story], list[Story]]:
    sources = cfg.source_map
    per_source: Counter[str] = Counter()
    per_section: Counter[str] = Counter()
    top: list[Story] = []
    rest: list[Story] = []

    for story in stories:
        if len(top) + len(rest) >= cfg.issue.max_stories:
            break
        source = sources[story.source_id]
        if per_source[story.source_id] >= (source.max_per_issue or cfg.issue.max_per_source):
            continue
        is_new_section = all(existing.section_id != story.section_id for existing in top)
        if len(top) < cfg.issue.top_stories and not source.community and is_new_section:
            top.append(story)
        elif per_section[story.section_id] < cfg.issue.max_per_section:
            rest.append(story)
            per_section[story.section_id] += 1
        else:
            continue
        per_source[story.source_id] += 1
    return top, rest
