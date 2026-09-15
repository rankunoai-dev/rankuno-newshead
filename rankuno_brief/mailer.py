"""Email delivery. Every recipient's delivery is recorded, so re-running a send never emails anyone twice."""

from __future__ import annotations

import logging
import mimetypes
import os
import smtplib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Protocol

from . import db
from .config import ConfigError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    sender_name: str

    @classmethod
    def from_env(cls) -> SmtpSettings:
        required = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_FROM")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise ConfigError(f"Email settings missing from .env: {', '.join(missing)} (see .env.example)")
        return cls(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            sender=os.environ["MAIL_FROM"],
            sender_name=os.environ.get("MAIL_FROM_NAME", ""),
        )


class Transport(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtpTransport:
    """SMTP over STARTTLS (port 587) or implicit TLS (port 465)."""

    def __init__(self, settings: SmtpSettings, timeout: float = 30.0) -> None:
        self._settings = settings
        self._timeout = timeout
        self._smtp: smtplib.SMTP | None = None

    def __enter__(self) -> SmtpTransport:
        s = self._settings
        if s.port == 465:
            self._smtp = smtplib.SMTP_SSL(s.host, s.port, timeout=self._timeout)
        else:
            self._smtp = smtplib.SMTP(s.host, s.port, timeout=self._timeout)
            self._smtp.starttls()
        self._smtp.login(s.username, s.password)
        return self

    def send(self, message: EmailMessage) -> None:
        assert self._smtp is not None, "SmtpTransport must be used as a context manager"
        self._smtp.send_message(message)

    def __exit__(self, *exc_info) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except smtplib.SMTPException:
                pass


def build_message(
    *,
    subject: str,
    html_body: str,
    text_body: str,
    sender: str,
    sender_name: str,
    recipient: str,
    inline_images: Mapping[str, Path] | None = None,
) -> EmailMessage:
    """multipart/alternative: plain text, then HTML with its embedded images (multipart/related)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    html_part = message.get_payload()[1]
    for content_id, path in (inline_images or {}).items():
        subtype = mimetypes.guess_type(path.name)[0].split("/")[1]
        html_part.add_related(
            path.read_bytes(), maintype="image", subtype=subtype, cid=f"<{content_id}>", filename=path.name
        )
    return message


@dataclass
class DeliveryReport:
    sent: list[str] = field(default_factory=list)
    already_sent: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.failed and not self.uncertain


def deliver_issue(
    conn: sqlite3.Connection,
    issue: sqlite3.Row,
    recipients: Iterable[str],
    transport: Transport,
    make_message: Callable[[str], EmailMessage],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> DeliveryReport:
    """Send an issue to each recipient exactly once.

    A recipient left in 'sending' means an earlier run stopped mid-send; we cannot tell whether the
    mail went out, so it is reported as uncertain instead of risking a duplicate.
    """
    report = DeliveryReport()
    for recipient in recipients:
        status = db.delivery_status(conn, issue["id"], recipient)
        if status == "sent":
            report.already_sent.append(recipient)
            continue
        if status == "sending":
            report.uncertain.append(recipient)
            continue

        db.mark_delivery(conn, issue["id"], recipient, "sending", now())
        try:
            transport.send(make_message(recipient))
        except (smtplib.SMTPException, OSError) as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            db.mark_delivery(conn, issue["id"], recipient, "failed", now(), error)
            report.failed.append((recipient, error))
            log.error("Delivery to %s failed: %s", recipient, error)
        else:
            db.mark_delivery(conn, issue["id"], recipient, "sent", now())
            report.sent.append(recipient)
            log.info("Delivered issue %s to %s", issue["issue_date"], recipient)

    if report.sent or report.already_sent:
        db.set_issue_status(conn, issue["id"], "sent" if report.complete else "partial", now())
    return report
