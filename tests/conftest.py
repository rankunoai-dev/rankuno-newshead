from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rankuno_brief import db
from rankuno_brief.config import ROOT, load_config

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def cfg():
    """The real project configuration, so tests also catch broken config files."""
    return load_config(ROOT)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def make_item(item_id: int, title: str, source_id: str, *, excerpt: str = "", published: str = "2026-09-12T10:00:00+00:00",
              url: str | None = None, via: str | None = None, image: str | None = None,
              publisher: str | None = None) -> dict:
    return {
        "id": item_id,
        "title": title,
        "source_id": source_id,
        "excerpt": excerpt,
        "published_at": published,
        "url": url or f"https://example.com/{item_id}",
        "discovered_via": via,
        "publisher": publisher,
        "image_url": image,
    }
