"""Configuration: YAML files in config/ plus secrets from .env or the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from . import slots

ROOT = Path(__file__).resolve().parent.parent

# rss: a publisher's own feed. Every other type is an aggregator: stories point at other publishers,
# and the email says where we found them ("Found via Google News").
SOURCE_TYPES = ("rss", "reddit", "techmeme", "google_alerts", "google_news", "hackernews")
FOUND_VIA = {
    "reddit": "Reddit",
    "techmeme": "Techmeme",
    "google_alerts": "Google Alerts",
    "google_news": "Google News",
    "hackernews": "Hacker News",
}
GOOGLE_NEWS_SEARCH_URL = "https://news.google.com/rss/search"
GOOGLE_NEWS_WINDOW = "2d"  # only stories from the last two days; the fetch runs daily
HACKERNEWS_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


class ConfigError(Exception):
    """Raised when a config file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    section: str
    type: str = "rss"
    url: str | None = None  # feed URL; built automatically for google_news and hackernews
    query: str | None = None  # google_news search query
    min_points: int = 50  # hackernews: only stories with at least this many points
    # Stories need this many publishers covering them, unless the publisher is trusted (sources.yaml).
    min_coverage: int = 1
    weight: float = 1.0
    require_keywords: bool = False
    community: bool = False
    max_per_issue: int | None = None
    enabled: bool = True

    @property
    def aggregator(self) -> bool:
        return self.type != "rss"

    @property
    def found_via(self) -> str | None:
        return FOUND_VIA.get(self.type)

    @property
    def feed_url(self) -> str:
        if self.type == "google_news":
            query = f"{self.query} when:{GOOGLE_NEWS_WINDOW}"
            return f"{GOOGLE_NEWS_SEARCH_URL}?{urlencode({'q': query, 'hl': 'en-US', 'gl': 'US', 'ceid': 'US:en'})}"
        if self.type == "hackernews":
            return HACKERNEWS_SEARCH_URL
        return self.url or ""

    @property
    def host(self) -> str:
        return urlsplit(self.feed_url).hostname or ""


@dataclass(frozen=True)
class Section:
    id: str
    title: str
    weight: float
    keywords: dict[str, float]


@dataclass(frozen=True)
class Taxonomy:
    sections: tuple[Section, ...]
    exclude_title_keywords: tuple[str, ...]
    evergreen_title_patterns: tuple[str, ...]
    evergreen_multiplier: float


@dataclass(frozen=True)
class NewsletterSettings:
    name: str
    tagline: str
    timezone: ZoneInfo
    website_url: str
    logo_path: str
    logo_white_path: str
    brand_tagline: str
    audience_note: str
    fetch_time: str
    send_slots: tuple[slots.SendSlot, ...]


@dataclass(frozen=True)
class IssueSettings:
    max_stories: int
    max_per_section: int
    max_per_source: int
    top_stories: int
    min_score: float
    first_issue_lookback_days: int
    grace_hours: int


@dataclass(frozen=True)
class FetchSettings:
    timeout_seconds: float
    retries: int
    max_workers: int
    same_host_delay_seconds: float
    max_item_age_days: int
    user_agent: str


@dataclass(frozen=True)
class DeliverySettings:
    recipients: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    root: Path
    newsletter: NewsletterSettings
    issue: IssueSettings
    fetch: FetchSettings
    delivery: DeliverySettings
    sources: tuple[Source, ...]
    taxonomy: Taxonomy
    exclude_publishers: tuple[str, ...] = ()  # names or domains, e.g. press-release wires
    trusted_publishers: tuple[str, ...] = ()  # names or domains exempt from a source's min_coverage

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "brief.db"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def source_map(self) -> dict[str, Source]:
        return {source.id: source for source in self.sources}

    @property
    def section_map(self) -> dict[str, Section]:
        return {section.id: section for section in self.taxonomy.sections}


def load_config(root: Path = ROOT) -> Config:
    load_env_file(root / ".env")
    settings = _read_yaml(root / "config" / "settings.yaml")
    sources_doc = _read_yaml(root / "config" / "sources.yaml")
    taxonomy_doc = _read_yaml(root / "config" / "taxonomy.yaml")

    try:
        taxonomy = _parse_taxonomy(taxonomy_doc)
        sources = tuple(Source(**entry) for entry in sources_doc["sources"])
        cfg = Config(
            root=root,
            newsletter=_parse_newsletter(settings["newsletter"]),
            issue=IssueSettings(**settings["issue"]),
            fetch=FetchSettings(**settings["fetch"]),
            delivery=DeliverySettings(recipients=tuple(settings["delivery"]["recipients"] or ())),
            sources=sources,
            taxonomy=taxonomy,
            exclude_publishers=tuple(str(entry) for entry in sources_doc.get("exclude_publishers") or ()),
            trusted_publishers=tuple(str(entry) for entry in sources_doc.get("trusted_publishers") or ()),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc

    _validate(cfg)
    return cfg


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding variables that are already set."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    with path.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    if not isinstance(document, dict):
        raise ConfigError(f"Config file is empty or not a mapping: {path}")
    return document


def _parse_newsletter(raw: dict) -> NewsletterSettings:
    values = dict(raw)
    try:
        values["timezone"] = ZoneInfo(values["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone: {raw['timezone']}") from exc
    values["send_slots"] = tuple(slots.parse_slot(text) for text in raw["send_slots"])
    return NewsletterSettings(**values)


def _parse_taxonomy(raw: dict) -> Taxonomy:
    sections = tuple(
        Section(
            id=entry["id"],
            title=entry["title"],
            weight=float(entry.get("weight", 1.0)),
            keywords={str(keyword): float(weight) for keyword, weight in (entry.get("keywords") or {}).items()},
        )
        for entry in raw["sections"]
    )
    return Taxonomy(
        sections=sections,
        exclude_title_keywords=tuple(raw.get("exclude_title_keywords") or ()),
        evergreen_title_patterns=tuple(raw.get("evergreen_title_patterns") or ()),
        evergreen_multiplier=float(raw.get("evergreen_multiplier", 1.0)),
    )


def _validate(cfg: Config) -> None:
    problems: list[str] = []

    section_ids = [section.id for section in cfg.taxonomy.sections]
    if len(section_ids) != len(set(section_ids)):
        problems.append("taxonomy.yaml has duplicate section ids")

    source_ids = [source.id for source in cfg.sources]
    duplicates = sorted({sid for sid in source_ids if source_ids.count(sid) > 1})
    if duplicates:
        problems.append(f"sources.yaml has duplicate ids: {', '.join(duplicates)}")

    for source in cfg.sources:
        if source.section not in section_ids:
            problems.append(f"source '{source.id}' uses unknown section '{source.section}'")
        if source.weight <= 0:
            problems.append(f"source '{source.id}' must have a positive weight")
        if source.type not in SOURCE_TYPES:
            problems.append(f"source '{source.id}' has unknown type '{source.type}' (use one of {', '.join(SOURCE_TYPES)})")
        elif source.type == "google_news":
            if not source.query:
                problems.append(f"source '{source.id}' (google_news) needs a query")
        elif source.type != "hackernews" and not (source.url or "").startswith(("http://", "https://")):
            problems.append(f"source '{source.id}' has a missing or invalid url")

    for logo in (cfg.newsletter.logo_path, cfg.newsletter.logo_white_path):
        if not (cfg.root / logo).is_file():
            problems.append(f"settings.yaml logo file not found: {logo}")

    if not cfg.newsletter.send_slots:
        problems.append("settings.yaml newsletter.send_slots must list at least one slot")
    if cfg.issue.top_stories > cfg.issue.max_stories:
        problems.append("settings.yaml issue.top_stories cannot exceed issue.max_stories")

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))
