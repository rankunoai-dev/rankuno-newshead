"""Settings for the personalised client feed, from config/personal.yaml and environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from ..config import ConfigError

PERSONAL_NAME = "The Client Brief"  # sender name, subjects and masthead of the client feed emails and pages

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


@dataclass(frozen=True)
class Industry:
    id: str
    title: str
    keywords: tuple[str, ...]
    competitors: tuple[str, ...]  # suggestions only
    keywords_default: tuple[str, ...]  # suggestions only


@dataclass(frozen=True)
class PersonalSettings:
    industries: dict[str, Industry]
    window_days: int
    max_stories: int
    max_per_source: int
    min_score: float
    run_weekday: int
    run_time: time
    session_days: int
    invite_days: int
    core_terms: tuple[str, ...] = ()
    operational_terms: tuple[str, ...] = ()
    broad_terms: tuple[str, ...] = ()

    def industry(self, industry_id: str) -> Industry | None:
        return self.industries.get(industry_id)


def load_settings(root: Path) -> PersonalSettings:
    path = root / "config" / "personal.yaml"
    if not path.exists():
        raise ConfigError(f"Missing {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feed = raw.get("feed") or {}
    industries: dict[str, Industry] = {}
    for entry in raw.get("industries") or []:
        industry = Industry(
            id=str(entry["id"]),
            title=str(entry["title"]),
            keywords=tuple(entry.get("keywords") or ()),
            competitors=tuple(entry.get("competitors") or ()),
            keywords_default=tuple(entry.get("keywords_default") or ()),
        )
        if industry.id in industries:
            raise ConfigError(f"Duplicate industry id in personal.yaml: {industry.id}")
        industries[industry.id] = industry
    if not industries:
        raise ConfigError("personal.yaml lists no industries")

    run_day = str(feed.get("run_day", "Mon")).strip().lower()[:3]
    if run_day not in _WEEKDAYS:
        raise ConfigError(f"personal.yaml feed.run_day must be a weekday, got {feed.get('run_day')!r}")
    hour, minute = (int(part) for part in str(feed.get("run_time", "10:00")).split(":"))
    return PersonalSettings(
        industries=industries,
        window_days=int(feed.get("window_days", 7)),
        max_stories=int(feed.get("max_stories", 10)),
        max_per_source=int(feed.get("max_per_source", 3)),
        min_score=float(feed.get("min_score", 1.5)),
        run_weekday=_WEEKDAYS[run_day],
        run_time=time(hour, minute),
        session_days=int(feed.get("session_days", 14)),
        invite_days=int(feed.get("invite_days", 7)),
        core_terms=tuple(str(term) for term in raw.get("core_terms") or ()),
        operational_terms=tuple(str(term) for term in raw.get("operational_terms") or ()),
        broad_terms=tuple(str(term) for term in raw.get("broad_terms") or ()),
    )


@lru_cache(maxsize=4)
def cached_settings(root: Path) -> PersonalSettings:
    return load_settings(root)


def public_base_url() -> str | None:
    """Where emailed links point, e.g. https://brief.up.railway.app. Without it the web pages stay off."""
    value = os.environ.get("PERSONAL_BASE_URL", "").strip().rstrip("/")
    if not value:
        return None
    parts = urlsplit(value)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ConfigError("PERSONAL_BASE_URL must be an https address (http is allowed only for localhost)")
    return value


def personal_sending_enabled() -> bool:
    """Weekly feeds and invitation emails go to real people only when this is true."""
    return os.environ.get("PERSONAL_SEND_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
