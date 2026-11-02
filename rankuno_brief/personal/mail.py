"""The emails of the personalised feed: the weekly client feed and the manager invitation. Plain HTML tables with
inline styles (Outlook-safe), a plain-text alternative, and every value escaped."""

from __future__ import annotations

from .settings import PERSONAL_NAME

import html
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

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


def weekly_email(cfg: Config, run_day: datetime, sections: list[ClientSection]) -> tuple[str, str]:
    """Returns (html, text). Logo: the white logo, embedded as cid:logo_white."""
    total = sum(len(section.stories) for section in sections)
    week = _date(run_day, cfg)
    names = [section.client_name for section in sections]
    intro_text = (
        f"This edition covers {_plural(len(sections), 'client')} and {_plural(total, 'story')}, "
        f"published in the seven days to {week}. Each story is checked for relevance to the client's "
        "industry, competitors and keywords."
    )

    # Introduction: one line per client.
    overview = "".join(
        f"""<tr><td style="padding:9px 0;border-bottom:1px solid #E6E6E6;font-family:{FONT};font-size:14px;color:{CHARCOAL};">{_e(section.client_name)}</td>
<td align="right" style="padding:9px 0;border-bottom:1px solid #E6E6E6;font-family:{FONT};font-size:13px;color:{GREY};">{_plural(len(section.stories), 'story').replace(' ', '&nbsp;')}</td></tr>"""
        for section in sections
    )
    blocks = [f"""<tr><td style="padding:24px 32px 6px 32px;font-family:{FONT};">
  <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:{RED};font-weight:bold;">In this edition</div>
  <p style="margin:8px 0 14px 0;font-size:14px;line-height:22px;color:{CHARCOAL};">{_e(intro_text)}</p>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{overview}</table>
</td></tr>"""]
    text_parts = [
        PERSONAL_NAME, f"Week of {week}", "",
        "IN THIS EDITION", intro_text, "",
    ]
    text_parts += [f"  {section.client_name}: {_plural(len(section.stories), 'story')}" for section in sections] + [""]

    for section in sections:
        themes = _themes(section.stories)
        industries = " · ".join(section.industries)
        theme_line = " · ".join(themes) if themes else "none this week"
        blocks.append(f"""<tr><td style="padding:26px 32px 4px 32px;border-top:2px solid {CHARCOAL};font-family:{FONT};">
  <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:{RED};font-weight:bold;">{_e(industries)}</div>
  <div style="font-size:20px;font-weight:bold;color:{CHARCOAL};margin-top:4px;">{_e(section.client_name)}</div>
  <div style="font-size:13px;color:{GREY};margin-top:6px;">Key themes: {_e(theme_line)}</div>
</td></tr>""")
        text_parts += [section.client_name.upper(), "-" * len(section.client_name)]
        if themes:
            text_parts += [f"Key themes: {theme_line}", ""]
        if not section.stories:
            blocks.append(f'<tr><td style="padding:14px 32px;font-family:{FONT};font-size:14px;color:{GREY};">No matching developments this week.</td></tr>')
            text_parts += ["No matching developments this week.", ""]
        for story in section.stories:
            summary = _summary(story.excerpt)
            summary_html = (f'<div style="font-size:14px;line-height:22px;color:{CHARCOAL};">{_e(summary)}</div>' if summary else "")
            relevance = " · ".join(story.reasons[:4])
            blocks.append(f"""<tr><td style="padding:16px 32px;border-top:1px solid #E6E6E6;font-family:{FONT};">
  <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:{GREY};">{_e(story.source)} &nbsp;·&nbsp; {_date(story.published, cfg)}</div>
  <a href="{_e(story.url)}" style="display:block;font-size:17px;line-height:23px;font-weight:bold;color:{CHARCOAL};text-decoration:none;margin:6px 0 8px 0;">{_e(story.title)}</a>
  {summary_html}
  <div style="font-size:12px;line-height:18px;color:{GREY};margin-top:10px;">Relevance: <span style="color:{RED};font-weight:bold;">{_e(relevance) if relevance else 'general industry news'}</span></div>
  <div style="margin-top:8px;"><a href="{_e(story.url)}" style="font-size:13px;color:{RED};font-weight:bold;text-decoration:none;">Read the full article &rarr;</a></div>
</td></tr>""")
            text_parts += [
                story.title, f"{story.source} · {_date(story.published, cfg)}", *([summary] if summary else []),
                f"Relevance: {relevance or 'general industry news'}", f"Read the full article: {story.url}", "",
            ]
        footer_links = ""
        if section.unsubscribe_url:
            footer_links = (f'<a href="{_e(section.unsubscribe_url)}" style="color:{GREY};text-decoration:underline;">'
                            f"Stop receiving {_e(section.client_name)} news</a>")
            text_parts += [f"Stop receiving {section.client_name} news: {section.unsubscribe_url}", ""]
        blocks.append(f'<tr><td style="padding:12px 32px 22px 32px;font-family:{FONT};font-size:12px;">{footer_links}</td></tr>')

    preheader = f"{_plural(total, 'story')} for {', '.join(names[:3])}{' and others' if len(names) > 3 else ''}, week of {week}."
    html_body = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_e(PERSONAL_NAME)}</title></head>
<body style="margin:0;padding:0;background:#F2F2F2;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{_e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F2F2;"><tr><td align="center" style="padding:24px 8px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:640px;background:#FFFFFF;border:1px solid #E6E6E6;">
<tr><td style="background:{RED};padding:22px 32px;font-family:{FONT};color:#FFFFFF;">
  <img src="cid:logo_white" alt="RankUno" height="30" style="display:block;border:0;" />
  <div style="font-size:11px;letter-spacing:1.5px;text-transform:uppercase;margin-top:16px;">Weekly client intelligence</div>
  <div style="font-size:22px;font-weight:bold;margin-top:4px;">{_e(PERSONAL_NAME)}</div>
  <div style="font-size:13px;margin-top:4px;">Week of {_e(week)}</div>
</td></tr>
{''.join(blocks)}
<tr><td style="padding:18px 32px;background:#F7F7F7;border-top:1px solid #E6E6E6;font-family:{FONT};font-size:11px;line-height:17px;color:{GREY};">
  Prepared for internal client planning within RankUno. Stories are drawn from public sources; each links to the original article.
  Questions about this brief can go to your account manager.
</td></tr>
</table>
</td></tr></table>
</body></html>"""
    text_parts += ["Prepared for internal client planning within RankUno. Stories link to their original articles."]
    return html_body, "\n".join(text_parts)


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
    return {"logo_white": cfg.root / cfg.newsletter.logo_white_path}


def notice(cfg: Config, *, subject: str, heading: str, paragraphs: list[str],
           button: tuple[str, str] | None = None) -> tuple[str, str, str]:
    """A short notice for an admin or manager: heading, paragraphs and one button. Returns (subject, html, text)."""
    kicker = "Client feeds · Notice"
    html_body = _shell(cfg, subject=subject, kicker=kicker, heading=heading, paragraphs=paragraphs, button=button)
    text_lines = [heading, "", *paragraphs, ""]
    if button:
        text_lines += [f"{button[0]}: {button[1]}", ""]
    return subject, html_body, "\n".join(text_lines)
