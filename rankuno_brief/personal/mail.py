"""The emails of the personalised feed: the weekly client feed and the manager invitation. Plain HTML tables with
inline styles (Outlook-safe), a plain-text alternative, and every value escaped."""

from __future__ import annotations

from .settings import PERSONAL_NAME

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..config import Config

RED, CHARCOAL, GREY = "#DF212A", "#262626", "#707070"
FONT = "Arial, Helvetica, sans-serif"


@dataclass(frozen=True)
class StoryLine:
    title: str
    url: str
    source: str
    published: datetime
    excerpt: str
    reasons: tuple[str, ...]
    badge: str = "editorial"  # editorial | sponsored | vendor
    strategic: bool = True  # a direct client or competitor match, rather than industry context
    image_cid: str | None = None  # the story picture, embedded as this Content-ID
    image_width: int = 0  # display width in pixels: 568 for a lead picture, 150 for a thumbnail
    image_height: int = 0


@dataclass(frozen=True)
class ClientSection:
    client_name: str
    industries: tuple[str, ...]
    stories: tuple[StoryLine, ...]
    unsubscribe_url: str | None


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _date(value: datetime, cfg: Config) -> str:
    return value.astimezone(cfg.newsletter.timezone).strftime("%d %b %Y")


def weekly_subject(cfg: Config, run_day: datetime) -> str:
    return f"{PERSONAL_NAME} | Week of {run_day.astimezone(cfg.newsletter.timezone):%d %B %Y}"


def _summary(text: str, max_words: int = 42) -> str:
    """The article's own opening text, tidied to a short summary. No AI: it is the start of the source text."""
    words = [word for word in " ".join((text or "").split()).split(" ") if word]
    if not words:
        return ""
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]).rstrip(",;:") + "…"


def _themes(stories) -> list[str]:
    """The distinct keywords that made these stories relevant, most frequent first."""
    counts: dict[str, int] = {}
    for story in stories:
        for reason in story.reasons:
            key = reason.lower()
            counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda pair: -pair[1])
    lookup = {reason.lower(): reason for story in stories for reason in story.reasons}
    return [lookup[key] for key, _ in ordered][:4]


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"{count} {word}"
    return f"{count} {'stories' if word == 'story' else word + 's'}"


BADGES = {
    "editorial": ("Editorial", "#707070"),
    "sponsored": ("Sponsored", "#8A6D00"),
    "vendor": ("Vendor report", "#1F4E79"),
}
_TEMPLATE = "personal_email.html.j2"


def _story_view(story: StoryLine, cfg: Config, client_name: str) -> dict:
    label, color = BADGES.get(story.badge, BADGES["editorial"])
    relevance = " · ".join(story.reasons[:4]) or "general industry news"
    why = (f"Why it matters to {client_name}: direct match on {relevance}." if story.strategic
           else f"Industry context: {relevance}.")
    return {
        "title": story.title,
        "url": story.url,
        "source": story.source,
        "date": _date(story.published, cfg),
        "excerpt": _summary(story.excerpt),
        "badge_label": label,
        "badge_color": color,
        "why": why,
        "image_src": f"cid:{story.image_cid}" if story.image_cid else None,
        "image_width": story.image_width or 150,
        "image_height": story.image_height or None,
    }


def _environment(cfg: Config) -> Environment:
    return Environment(
        loader=FileSystemLoader(cfg.templates_dir),
        autoescape=lambda name: bool(name) and name.endswith(".html.j2"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def weekly_email(cfg: Config, run_day: datetime, sections: list[ClientSection]) -> tuple[str, str]:
    """Returns (html, text). Images are referenced as cid: parts that the sender attaches (see feed.inline_images)."""
    total = sum(len(section.stories) for section in sections)
    week = _date(run_day, cfg)
    names = [section.client_name for section in sections]
    intro_text = (
        f"This edition covers {_plural(len(sections), 'client')} and {_plural(total, 'story')}, published in the seven "
        f"days to {week}. Each story is checked for relevance to the client's industry, competitors and keywords. "
        "Sponsored and vendor pieces are labelled."
    )
    clients = []
    for section in sections:
        themes = _themes(section.stories)
        clients.append({
            "name": section.client_name,
            "industries": " · ".join(section.industries),
            "themes": " · ".join(themes) if themes else "none this week",
            "unsubscribe_url": section.unsubscribe_url,
            "strategic": [_story_view(story, cfg, section.client_name) for story in section.stories if story.strategic],
            "industry": [_story_view(story, cfg, section.client_name) for story in section.stories if not story.strategic],
        })
    preheader = f"{_plural(total, 'story')} for {', '.join(names[:3])}{' and others' if len(names) > 3 else ''}, week of {week}."
    html_body = _environment(cfg).get_template(_TEMPLATE).render(
        title=PERSONAL_NAME,
        week=week,
        preheader=preheader,
        intro=intro_text,
        coverage=f"{_plural(len(sections), 'client')} · {_plural(total, 'story')}",
        toc=[{"name": section.client_name, "count": _plural(len(section.stories), "story")} for section in sections],
        clients=clients,
        logo="cid:logo",
        logo_white="cid:logo_white",
        website_url=cfg.newsletter.website_url,
        brand_tagline=cfg.newsletter.brand_tagline,
    )
    return html_body, _text_version(cfg, week, intro_text, sections)


def _text_version(cfg: Config, week: str, intro_text: str, sections: list[ClientSection]) -> str:
    parts = [PERSONAL_NAME, f"Week of {week}", "", "IN THIS EDITION", intro_text, ""]
    parts += [f"  {section.client_name}: {_plural(len(section.stories), 'story')}" for section in sections] + [""]
    for section in sections:
        themes = _themes(section.stories)
        parts += [section.client_name.upper(), "-" * len(section.client_name)]
        parts += [f"Key themes: {' · '.join(themes)}", ""] if themes else []
        if not section.stories:
            parts += ["No matching developments this week.", ""]
        for group_title, stories in (
            ("Strategic client & market moves", [story for story in section.stories if story.strategic]),
            ("Industry & vendor intelligence", [story for story in section.stories if not story.strategic]),
        ):
            if not stories:
                continue
            parts += [group_title.upper(), ""]
            for story in stories:
                label, _ = BADGES.get(story.badge, BADGES["editorial"])
                view = _story_view(story, cfg, section.client_name)
                parts += [f"[{label.upper()}] {story.title}", f"{story.source} · {view['date']}"]
                parts += [view["excerpt"]] if view["excerpt"] else []
                parts += [view["why"], f"Read the full article: {story.url}", ""]
        if section.unsubscribe_url:
            parts += [f"Stop receiving {section.client_name} news: {section.unsubscribe_url}", ""]
    parts += ["Prepared for internal client planning within RankUno. Stories link to their original articles."]
    return "\n".join(parts)


def invite_subject(cfg: Config) -> str:
    return f"{PERSONAL_NAME}: your sign-in link"


def _masthead(cfg: Config, kicker: str) -> str:
    """The newsletter's red masthead with the white logo (embedded as cid:logo_white)."""
    return f"""<tr><td style="background:{RED};padding:18px 28px;font-family:{FONT};color:#FFFFFF;">
  <img src="cid:logo_white" alt="RankUno" height="32" style="display:block;border:0;" />
  <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;margin-top:12px;">{_e(kicker)}</div>
  <div style="font-size:17px;font-weight:bold;margin-top:2px;">{_e(PERSONAL_NAME)}</div>
</td></tr>"""


def _button(label: str, url: str) -> str:
    """A bulletproof button: a red table cell with the link inside. Link colour is set on the text itself,
    so email clients cannot recolour it."""
    return f"""<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0 8px 0;"><tr>
  <td bgcolor="{RED}" style="background:{RED};padding:13px 24px;">
    <a href="{_e(url)}" style="font-family:{FONT};font-size:14px;font-weight:bold;text-decoration:none;color:#FFFFFF;"><span style="color:#FFFFFF;">{_e(label)}</span></a>
  </td></tr></table>"""


def _shell(cfg: Config, *, subject: str, kicker: str, heading: str, paragraphs: list[str],
           button: tuple[str, str] | None, footnote: str = "") -> str:
    paras = "".join(
        f'<p style="margin:0 0 12px 0;font-family:{FONT};font-size:14px;line-height:22px;color:{CHARCOAL};">{_e(text)}</p>'
        for text in paragraphs
    )
    cta = _button(*button) if button else ""
    note = (f'<p style="margin:14px 0 0 0;font-family:{FONT};font-size:12px;line-height:18px;color:{GREY};">{_e(footnote)}</p>'
            if footnote else "")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_e(subject)}</title></head>
<body style="margin:0;padding:0;background:#F2F2F2;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F2F2;"><tr><td align="center" style="padding:24px 8px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:600px;background:#FFFFFF;border:1px solid #E6E6E6;">
{_masthead(cfg, kicker)}
<tr><td style="padding:26px 28px 24px 28px;">
  <div style="font-family:{FONT};font-size:20px;font-weight:bold;color:{CHARCOAL};margin:0 0 14px 0;">{_e(heading)}</div>
  {paras}
  {cta}
  {note}
</td></tr>
<tr><td style="padding:14px 28px;background:#F7F7F7;border-top:1px solid #E6E6E6;font-family:{FONT};font-size:11px;line-height:17px;color:{GREY};">
  Prepared for internal circulation within RankUno. Client feeds are separate from the general brief.
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def invite_email(cfg: Config, name: str, link: str, days: int) -> tuple[str, str]:
    greeting = f"Hello {name}," if name else "Hello,"
    paragraphs = [
        greeting,
        "You have been invited to set up the weekly news feed for your clients. Each Monday, every client's "
        "feed goes to the people you add: news on its industry, its competitors and its keywords.",
        "Use the button below to sign in. Then add your clients and submit them for approval.",
    ]
    html_body = _shell(
        cfg, subject=invite_subject(cfg), kicker="Sign-in invitation", heading="Set up your client feeds",
        paragraphs=paragraphs, button=("Sign in and set up your clients", link),
        footnote=f"This link works once and expires in {days} days. If you did not expect this email, ignore it.",
    )
    text = (
        f"{greeting}\n\n"
        "You have been invited to set up the weekly news feed for your clients.\n\n"
        f"Sign in and set up your clients: {link}\n\n"
        f"This link works once and expires in {days} days. If you did not expect this email, ignore it.\n"
    )
    return html_body, text


def logo_inline(cfg: Config) -> dict[str, Path]:
    return {"logo": cfg.root / cfg.newsletter.logo_path, "logo_white": cfg.root / cfg.newsletter.logo_white_path}


def notice(cfg: Config, *, subject: str, heading: str, paragraphs: list[str],
           button: tuple[str, str] | None = None) -> tuple[str, str, str]:
    """A short notice for an admin or manager: heading, paragraphs and one button. Returns (subject, html, text)."""
    kicker = "Client feeds · Notice"
    html_body = _shell(cfg, subject=subject, kicker=kicker, heading=heading, paragraphs=paragraphs, button=button)
    text_lines = [heading, "", *paragraphs, ""]
    if button:
        text_lines += [f"{button[0]}: {button[1]}", ""]
    return subject, html_body, "\n".join(text_lines)
