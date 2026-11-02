"""Emails to people other than the one signed in: approval requests, decisions, edit notices, sign-in links.

Nothing is emailed unless PERSONAL_SEND_ENABLED is true. Every recipient gets their own one-time sign-in link,
so a link is never shared between people and cannot be used twice.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime

from ..config import Config
from ..mail_profiles import PRODUCTION, load_profile
from . import feed, store
from .settings import PersonalSettings, personal_sending_enabled

log = logging.getLogger(__name__)


def send(cfg: Config, to: str, subject: str, html_body: str, text_body: str) -> bool:
    if not personal_sending_enabled():
        log.info("Not emailed to %s (PERSONAL_SEND_ENABLED is off): %s", to, subject)
        return False
    try:
        profile = load_profile(PRODUCTION, cfg)
        message = feed._message(profile, cfg, subject, html_body, text_body, to)
        with profile.transport() as transport:
            transport.send(message)
    except Exception as exc:  # noqa: BLE001 - a failed notice must not undo the decision it reports
        log.error("Email to %s failed: %s", to, exc)
        return False
    return True


def sign_in_link(conn: sqlite3.Connection, settings: PersonalSettings, base: str, person_id: int, now: datetime) -> str:
    raw = store.create_invite(conn, person_id, "system", now, settings.invite_days)
    return f"{base}/m/{raw}"


def notify_people(
    conn: sqlite3.Connection, cfg: Config, settings: PersonalSettings, base: str, now: datetime,
    people: Iterable[sqlite3.Row], build: Callable[[str, sqlite3.Row], tuple[str, str, str]],
) -> int:
    """Emails each person. build(link, person) returns (subject, html, text)."""
    if not personal_sending_enabled():
        log.info("Notice not emailed (PERSONAL_SEND_ENABLED is off)")
        return 0
    sent = 0
    for person in people:
        link = sign_in_link(conn, settings, base, person["id"], now)
        subject, html_body, text_body = build(link, person)
        if send(cfg, person["email"], subject, html_body, text_body):
            sent += 1
    return sent


def admins(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return [row for row in store.people(conn) if row["role"] == "admin" and row["active"]]
