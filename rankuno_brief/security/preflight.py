"""Everything checked before an issue is sent. Any error stops the send; warnings are reported.

1. Integrity       the email files are exactly what the build produced and screened
2. Output gate     no blocked term, active content or unsafe link anywhere in the email
3. Recipients      valid, allowed, reachable and not suppressed
4. Spam signals    subject, headers, links, images, size, plain-text version
5. Sender          SPF / DKIM / DMARC for the sending domain (DNS problems only warn)
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage

from .. import db
from . import content, deliverability, recipients
from .dns import DnsResolver
from .findings import Finding, error, has_errors

CHECKS = ("integrity", "content", "recipients", "spam-signals", "sender-auth")
PLACEHOLDER_RECIPIENT = "preview@rankuno.com"


@dataclass
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)
    recipients: recipients.RecipientCheck = field(default_factory=recipients.RecipientCheck)
    sender: deliverability.SenderReport | None = None
    checks: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return has_errors(self.findings)

    def for_check(self, check: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.check == check]


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def html_fingerprint(html_body: str, images: Mapping[str, bytes] | None = None) -> str:
    """Fingerprint of the HTML and every image embedded in it, so a swapped picture is caught too.
    Without images it equals sha256(html_body)."""
    digest = hashlib.sha256(html_body.encode("utf-8"))
    for cid in sorted(images or {}):
        digest.update(f"\n{cid}:".encode())
        digest.update(hashlib.sha256(images[cid]).digest())
    return digest.hexdigest()


def run_preflight(
    cfg,
    conn: sqlite3.Connection,
    issue: sqlite3.Row | None,
    *,
    html_body: str,
    text_body: str,
    requested_recipients: Iterable[str],
    make_message: Callable[[str], EmailMessage],
    sender: str | None,
    smtp_host: str | None,
    resolver: DnsResolver | None,
    images: Mapping[str, bytes] | None = None,
) -> PreflightReport:
    """Without an issue (none built yet) only the recipients and the sender are checked."""
    report = PreflightReport(checks=list(CHECKS))
    gate = content.gate_for(cfg)

    if issue is not None:
        report.findings.extend(check_integrity(issue, html_body, text_body, images))
        report.findings.extend(gate.scan_email(issue["subject"], html_body, text_body))

    report.recipients = recipients.check_recipients(
        requested_recipients,
        cfg.security.recipients,
        suppressed=db.suppressed_addresses(conn),
        resolver=resolver,
    )
    report.findings.extend(report.recipients.findings)

    if issue is not None:
        sample = make_message(report.recipients.accepted[0] if report.recipients.accepted else PLACEHOLDER_RECIPIENT)
        report.findings.extend(deliverability.lint_message(sample, html_body=html_body, text_body=text_body, gate=gate))
    else:
        report.checks = [check for check in report.checks if check not in ("integrity", "content", "spam-signals")]

    if sender and smtp_host and resolver is not None:
        report.sender = deliverability.check_sender(sender, smtp_host, resolver)
        report.findings.extend(report.sender.findings)
    else:
        report.checks.remove("sender-auth")
    return report


def check_integrity(
    issue: sqlite3.Row, html_body: str, text_body: str, images: Mapping[str, bytes] | None = None
) -> list[Finding]:
    """The files and embedded images must match the fingerprints recorded when the build screened them."""
    expected_html, expected_text = issue["html_sha256"], issue["text_sha256"]
    if not expected_html or not expected_text:
        return [error("integrity", f"Issue {issue['issue_date']} was built before the security layer existed. Run 'build' again.")]
    if html_fingerprint(html_body, images) != expected_html or sha256(text_body) != expected_text:
        return [
            error(
                "integrity",
                f"The email files for {issue['issue_date']} changed after they were built and screened. "
                "Run 'build' again rather than editing them by hand.",
            )
        ]
    return []
