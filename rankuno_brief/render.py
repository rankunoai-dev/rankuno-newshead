"""Renders issue content into the HTML email and its plain-text alternative."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .compose import IssueContent
from .config import Config
from .images import EmbeddedImage

GMAIL_CLIP_BYTES = 102_000  # Gmail hides everything after ~102 KB of HTML behind "View entire message"


@dataclass(frozen=True)
class IssueMeta:
    number: int
    issue_date: date
    window_start: datetime
    window_end: datetime
    subject: str


def make_subject(cfg: Config, issue_date: date) -> str:
    return f"{cfg.newsletter.name} | {long_date(issue_date)}"


def inline_images(cfg: Config) -> dict[str, Path]:
    """Images embedded in every email, keyed by Content-ID."""
    return {
        "logo": cfg.root / cfg.newsletter.logo_path,
        "logo_white": cfg.root / cfg.newsletter.logo_white_path,
    }


def render_issue(
    content: IssueContent,
    meta: IssueMeta,
    cfg: Config,
    *,
    preview: bool = False,
    story_images: Mapping[int, EmbeddedImage] | None = None,
) -> tuple[str, str]:
    """Render the HTML and plain-text bodies.

    Emails reference embedded images as cid: links; a preview (for opening in a browser)
    points at the image files on disk instead. Without `story_images`, stories link their
    original image addresses (used for quick offline previews and tests).
    """
    env = _environment(cfg)
    tz = cfg.newsletter.timezone
    images = {
        name: (path.resolve().as_uri() if preview else f"cid:{name}") for name, path in inline_images(cfg).items()
    }

    def story_image(story, display_width: int) -> dict | None:
        if story_images is None:
            return {"src": story.image_url, "height": None} if story.image_url else None
        embedded = story_images.get(story.item_id)
        if embedded is None:
            return None
        src = embedded.path.resolve().as_uri() if preview else f"cid:{embedded.cid}"
        return {"src": src, "height": embedded.display_height(display_width)}

    context = {
        "cfg": cfg.newsletter,
        "meta": meta,
        "content": content,
        "images": images,
        "story_image": story_image,
        "coverage": coverage_label(meta.window_start.astimezone(tz).date(), meta.window_end.astimezone(tz).date()),
        "preheader": _preheader(content),
    }
    html_body = env.get_template("email.html.j2").render(**context)
    text_body = env.get_template("email.txt.j2").render(**context)
    return html_body, text_body


def long_date(value: date | datetime) -> str:
    return f"{value:%A}, {value.day} {value:%B %Y}"


def short_date(value: date | datetime) -> str:
    return f"{value.day} {value:%b %Y}"


def coverage_label(start: date, end: date) -> str:
    if start == end:
        return f"{end.day} {end:%B %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} – {end.day} {end:%B %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%B} – {end.day} {end:%B %Y}"
    return f"{start.day} {start:%B %Y} – {end.day} {end:%B %Y}"


def _preheader(content: IssueContent) -> str:
    if content.top_stories:
        return f"This issue leads with: {content.top_stories[0].title}"
    return "The latest search, AI and marketing developments."


def _environment(cfg: Config) -> Environment:
    env = Environment(
        loader=FileSystemLoader(cfg.templates_dir),
        autoescape=lambda name: bool(name) and name.endswith(".html.j2"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tz = cfg.newsletter.timezone
    env.filters["short_date"] = lambda value: short_date(value.astimezone(tz) if isinstance(value, datetime) else value)
    env.filters["long_date"] = long_date
    env.filters["host"] = lambda url: (urlsplit(url).hostname or "").removeprefix("www.")
    return env
