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


def weekly_email(cfg: Config, run_day: datetime, sections: list[ClientSection]) -> tuple[str, str]:
    """Returns (html, text). Logo: embed cid:logo_white (inline image, sent with the message)."""
    blocks = []
    text_parts = [f"{PERSONAL_NAME}", f"Week of {_date(run_day, cfg)}", ""]
    for section in sections:
        blocks.append(
            f"""<tr><td style="padding:22px 28px 6px 28px;font-family:{FONT};">
  <div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:{RED};font-weight:bold;">{_e(', '.join(section.industries))}</div>
  <div style="font-size:20px;color:{CHARCOAL};font-weight:bold;margin-top:4px;">{_e(section.client_name)}</div>
</td></tr>"""
        )
        text_parts += [section.client_name.upper(), "-" * len(section.client_name)]
        for story in section.stories:
            blocks.append(
                f"""<tr><td style="padding:12px 28px;border-top:1px solid #E6E6E6;font-family:{FONT};">
  <a href="{_e(story.url)}" style="color:{CHARCOAL};font-size:16px;font-weight:bold;text-decoration:none;">{_e(story.title)}</a>
  <div style="font-size:12px;color:{GREY};margin-top:4px;">{_e(story.source)} · {_date(story.published, cfg)}</div>
  <div style="font-size:14px;line-height:21px;color:{CHARCOAL};margin-top:6px;">{_e(story.excerpt)}</div>
  <div style="margin-top:6px;"><a href="{_e(story.url)}" style="color:{RED};font-size:13px;">Read the article</a></div>
</td></tr>"""
            )
            text_parts += [f"* {story.title}", f"  {story.source} · {_date(story.published, cfg)}", f"  {story.excerpt}", f"  {story.url}", ""]
        if not section.stories:
            blocks.append(f'<tr><td style="padding:12px 28px;font-family:{FONT};font-size:14px;color:{GREY};">No matching news this week.</td></tr>')
        footer_links = ""
        if section.unsubscribe_url:
            footer_links = (
                f'<a href="{_e(section.unsubscribe_url)}" style="color:{GREY};">'
                f"Stop receiving {_e(section.client_name)} news</a>"
            )
            text_parts += [f"Stop receiving {section.client_name} news: {section.unsubscribe_url}", ""]
        blocks.append(f'<tr><td style="padding:0 28px 16px 28px;font-family:{FONT};font-size:12px;">{footer_links}</td></tr>')

    html_body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_e(PERSONAL_NAME)}</title></head>
<body style="margin:0;padding:0;background:#F2F2F2;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F2F2;"><tr><td align="center" style="padding:20px 8px;">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="width:640px;max-width:640px;background:#FFFFFF;border-top:5px solid {RED};">
<tr><td style="background:{RED};padding:18px 28px;font-family:{FONT};color:#FFFFFF;">
  <img src="cid:logo_white" alt="RankUno" height="32" style="display:block;border:0;" />
  <div style="font-size:18px;font-weight:bold;margin-top:10px;">{_e(PERSONAL_NAME)}</div>
  <div style="font-size:13px;margin-top:2px;">Week of {_e(_date(run_day, cfg))}</div>
</td></tr>
{''.join(blocks)}
<tr><td style="padding:18px 28px;background:#F7F7F7;font-family:{FONT};font-size:11px;color:{GREY};">
  Prepared for internal circulation within RankUno. Each client's stories are selected from the same news as the
  general brief; a story can appear in both.
</td></tr>
</table></td></tr></table>
</body></html>"""
    text_parts += ["Prepared for internal circulation within RankUno."]
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
