"""Email delivery. Every recipient's delivery is recorded, so re-running a send never emails anyone twice.

Two ways to send, chosen by MAIL_PROVIDER (see mail_profiles.py):
    smtp    any SMTP server, e.g. Gmail with an App Password (port 587 or 465)
    graph   Microsoft 365 through the Microsoft Graph API (HTTPS only, so it works where SMTP ports are blocked)
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import smtplib
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from . import db

log = logging.getLogger(__name__)


class MailError(Exception):
    """The message could not be sent. Unless it is a RecipientRejected, the rest of the send stops."""


class AuthError(MailError):
    """The mail service rejected our credentials or permissions."""


class RecipientRejected(MailError):
    """Only this address was refused; sending to the others continues."""

    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent  # e.g. "mailbox does not exist", as opposed to "mailbox full, try later"


class Transport(Protocol):
    def send(self, message: EmailMessage) -> None: ...


# SMTP -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str


class SmtpTransport:
    """SMTP over STARTTLS (port 587) or implicit TLS (port 465)."""

    def __init__(self, settings: SmtpSettings, timeout: float = 30.0) -> None:
        self._settings = settings
        self._timeout = timeout
        self._smtp: smtplib.SMTP | None = None

    def __enter__(self) -> SmtpTransport:
        s = self._settings
        try:
            if s.port == 465:
                self._smtp = smtplib.SMTP_SSL(s.host, s.port, timeout=self._timeout)
            else:
                self._smtp = smtplib.SMTP(s.host, s.port, timeout=self._timeout)
                self._smtp.starttls()
            self._smtp.login(s.username, s.password)
        except smtplib.SMTPAuthenticationError as exc:
            server_message = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
            raise AuthError(
                f"The mail server rejected the login ({exc.smtp_code}: {' '.join(server_message.split())}). Check "
                "SMTP_USERNAME and SMTP_PASSWORD; for Gmail the password must be an App Password of that account."
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(
                f"Could not connect to {s.host}:{s.port} ({type(exc).__name__}: {exc}). Some hosts, including "
                "Railway's Free and Hobby plans, block SMTP ports; use MAIL_PROVIDER=graph there."
            ) from exc
        return self

    def send(self, message: EmailMessage) -> None:
        assert self._smtp is not None, "SmtpTransport must be used as a context manager"
        try:
            self._smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [code for code, _ in exc.recipients.values()]
            raise RecipientRejected(f"SMTPRecipientsRefused: {exc}", permanent=any(code >= 500 for code in codes)) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"{type(exc).__name__}: {exc}") from exc

    def __exit__(self, *exc_info) -> None:
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass


# Microsoft Graph --------------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphSettings:
    tenant_id: str
    client_id: str
    client_secret: str


class GraphTransport:
    """Sends the finished MIME message through Microsoft Graph as the MAIL_FROM mailbox (app-only, Mail.Send)."""

    TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    SEND_URL = "https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"
    SCOPE = "https://graph.microsoft.com/.default"
    RETRYABLE = frozenset({429, 500, 502, 503, 504})
    MAX_RETRY_AFTER = 60.0

    def __init__(
        self,
        settings: GraphSettings,
        mailbox: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._mailbox = mailbox
        self._own_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._retries = retries
        self._sleep = sleep
        self._token: str | None = None
        self._token_expires = 0.0

    def __enter__(self) -> GraphTransport:
        self._refresh_token()  # fail before the first recipient if the credentials are wrong
        return self

    def __exit__(self, *exc_info) -> None:
        if self._own_client:
            self._client.close()

    def send(self, message: EmailMessage) -> None:
        body = base64.b64encode(mime_bytes(message))
        url = self.SEND_URL.format(mailbox=quote(self._mailbox, safe="@"))
        reauthenticated = False
        for attempt in range(self._retries + 1):
            if time.monotonic() > self._token_expires - 300:
                self._refresh_token()
            try:
                response = self._client.post(
                    url, content=body, headers={"Authorization": f"Bearer {self._token}", "Content-Type": "text/plain"}
                )
            except httpx.TransportError as exc:
                if attempt < self._retries:
                    self._sleep(2.0 ** (attempt + 1))
                    continue
                raise MailError(f"Could not reach Microsoft Graph ({type(exc).__name__}: {exc})") from exc

            if response.status_code == 202:
                return
            if response.status_code == 401 and not reauthenticated:
                reauthenticated = True
                self._refresh_token()
                continue
            if response.status_code in self.RETRYABLE and attempt < self._retries:
                self._sleep(_retry_after(response, default=2.0 ** (attempt + 1), cap=self.MAX_RETRY_AFTER))
                continue
            raise self._error_for(response)
        raise MailError("Microsoft Graph kept asking us to retry; the send was stopped")

    def _refresh_token(self) -> None:
        s = self._settings
        try:
            response = self._client.post(
                self.TOKEN_URL.format(tenant=quote(s.tenant_id, safe="")),
                data={
                    "client_id": s.client_id,
                    "client_secret": s.client_secret,
                    "scope": self.SCOPE,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.TransportError as exc:
            raise MailError(f"Could not reach Microsoft sign-in ({type(exc).__name__}: {exc})") from exc
        payload = _json(response)
        if response.status_code != 200 or not payload.get("access_token"):
            description = str(payload.get("error_description") or response.text).splitlines()[0][:300]
            raise AuthError(
                f"Microsoft sign-in failed ({payload.get('error', response.status_code)}): {description}. "
                "Check GRAPH_TENANT_ID, GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET (secrets expire)."
            )
        self._token = payload["access_token"]
        self._token_expires = time.monotonic() + float(payload.get("expires_in", 3600))

    def _error_for(self, response: httpx.Response) -> MailError:
        error = _json(response).get("error") or {}
        code, detail = error.get("code", ""), str(error.get("message", response.text))[:300]
        summary = f"Microsoft Graph refused the message (HTTP {response.status_code} {code}): {detail}"
        if response.status_code == 403 or code == "ErrorAccessDenied":
            return AuthError(
                f"{summary}. The app needs the Mail.Send application permission with admin consent, and any "
                f"application access policy must include {self._mailbox}."
            )
        if response.status_code == 404 or code in ("ErrorInvalidUser", "MailboxNotEnabledForRESTAPI", "ResourceNotFound"):
            return AuthError(f"{summary}. MAIL_FROM ({self._mailbox}) must be an existing Microsoft 365 mailbox.")
        if code == "ErrorInvalidRecipients":
            return RecipientRejected(summary, permanent=True)
        return MailError(summary)


def _json(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _retry_after(response: httpx.Response, *, default: float, cap: float) -> float:
    try:
        return max(0.0, min(float(response.headers.get("retry-after", default)), cap))
    except ValueError:
        return default


# Messages and delivery ---------------------------------------------------------------------------


def mime_bytes(message: EmailMessage) -> bytes:
    """The message exactly as it goes on the wire: CRLF line endings, as email standards and Microsoft Exchange require.

    EmailMessage.as_bytes() uses bare LF line endings by default. Exchange and Outlook then misread every
    quoted-printable line wrap ("=" at the end of a line) and drop the character after it, which garbles the
    text ("=hursday", "&=bsp;") and breaks image and link addresses. smtplib converts to CRLF itself; Graph
    and .eml files get these bytes.
    """
    return message.as_bytes(policy=message.policy.clone(linesep="\r\n"))


def build_message(
    *,
    subject: str,
    html_body: str,
    text_body: str,
    sender: str,
    sender_name: str,
    recipient: str,
    inline_images: Mapping[str, Path] | None = None,
    reply_to: str | None = None,
    unsubscribe_mailbox: str | None = None,
) -> EmailMessage:
    """multipart/alternative: plain text, then HTML with its embedded images (multipart/related)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    message["To"] = recipient
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    if reply_to:
        message["Reply-To"] = reply_to
    if unsubscribe_mailbox:
        # Gmail and Outlook show their own "Unsubscribe" link for this, which people use instead of "Report spam".
        message["List-Unsubscribe"] = f"<mailto:{unsubscribe_mailbox}?subject=Unsubscribe>"
    message["X-Auto-Response-Suppress"] = "OOF, AutoReply"  # no out-of-office replies from Outlook
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    html_part = message.get_payload()[1]
    for content_id, path in (inline_images or {}).items():
        subtype = mimetypes.guess_type(path.name)[0].split("/")[1]
        # "inline", so mail clients show the pictures in the email rather than listing them as attachments
        html_part.add_related(
            path.read_bytes(), maintype="image", subtype=subtype, cid=f"<{content_id}>", filename=path.name,
            disposition="inline",
        )
    return message


@dataclass
class DeliveryReport:
    sent: list[str] = field(default_factory=list)
    already_sent: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    not_attempted: list[str] = field(default_factory=list)
    stopped_because: str | None = None

    @property
    def complete(self) -> bool:
        return not self.failed and not self.uncertain and not self.not_attempted


def deliver_issue(
    conn: sqlite3.Connection,
    issue: sqlite3.Row,
    recipients: Iterable[str],
    transport: Transport,
    make_message: Callable[[str], EmailMessage],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    *,
    pause_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    suppress_after: int | None = None,
) -> DeliveryReport:
    """Send an issue to each recipient exactly once.

    A recipient left in 'sending' means an earlier run stopped mid-send; we cannot tell whether the
    mail went out, so it is reported as uncertain instead of risking a duplicate.

    Messages are spaced `pause_seconds` apart. If the service refuses the message itself (spam or policy
    rejection, sending limit, lost connection), the run stops: pushing the same message at the rest of
    the list would only damage the sender's reputation. Addresses rejected permanently `suppress_after`
    times in a row are suppressed.
    """
    report = DeliveryReport()
    attempted = False
    for recipient in recipients:
        status = db.delivery_status(conn, issue["id"], recipient)
        if status == "sent":
            report.already_sent.append(recipient)
            continue
        if status == "sending":
            report.uncertain.append(recipient)
            continue
        if report.stopped_because:
            report.not_attempted.append(recipient)
            continue

        if attempted and pause_seconds:
            sleep(pause_seconds)
        attempted = True
        db.mark_delivery(conn, issue["id"], recipient, "sending", now())
        try:
            transport.send(make_message(recipient))
        except MailError as exc:
            error = str(exc)[:500]
            db.mark_delivery(conn, issue["id"], recipient, "failed", now(), error)
            report.failed.append((recipient, error))
            log.error("Delivery to %s failed: %s", recipient, error)
            if not isinstance(exc, RecipientRejected):
                report.stopped_because = error
                log.error("Stopping this send: the mail service refused the message itself, not just one address")
            elif suppress_after and exc.permanent:
                if db.record_hard_failure(conn, recipient, error, now(), suppress_after):
                    log.warning("Suppressed %s after %d permanent rejections", recipient, suppress_after)
        else:
            db.mark_delivery(conn, issue["id"], recipient, "sent", now())
            db.clear_hard_failures(conn, recipient)
            report.sent.append(recipient)
            log.info("Delivered issue %s to %s", issue["issue_date"], recipient)

    if report.sent or report.already_sent:
        db.set_issue_status(conn, issue["id"], "sent" if report.complete else "partial", now())
    return report
