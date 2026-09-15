"""Deliverability: keeps the brief out of spam folders.

lint_message   spam signals in the message itself: subject, headers, links, images, size, text version.
check_sender   whether the sender's domain authorises the mail server in use (SPF, DKIM, DMARC). This is
               the biggest single factor in inbox placement, and it is fixed in DNS, not in code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from urllib.parse import urlsplit

from .. import text
from .content import ContentGate
from .dns import DnsError, DnsResolver
from .findings import Finding, error, warning

GMAIL_CLIP_BYTES = 102_000
MAX_LINKS = 150
MAX_REMOTE_IMAGES = 40

_ANCHOR = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*(["'])(.*?)\1[^>]*>(.*?)</a>""", re.IGNORECASE | re.DOTALL)
_HREF = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_URL_LIKE_TEXT = re.compile(r"(?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?", re.IGNORECASE)
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿]")
_SPF_REFERENCES = ("include:", "+include:", "redirect=")


@dataclass(frozen=True)
class MailProvider:
    name: str
    smtp_hosts: tuple[str, ...]  # host names (or their parent domains) of the provider's mail servers
    spf_includes: tuple[str, ...]  # what a customer domain's SPF record must include
    dkim_selectors: tuple[str, ...]  # usual DKIM selectors; empty when they are account-specific
    own_domains: tuple[str, ...] = ()  # addresses the provider signs itself (always aligned)


PROVIDERS = (
    MailProvider("Google", ("smtp.gmail.com", "smtp-relay.gmail.com"), ("_spf.google.com",), ("google",), ("gmail.com", "googlemail.com")),
    MailProvider(
        "Microsoft 365",
        ("smtp.office365.com", "smtp-mail.outlook.com", "outlook.office365.com", "graph.microsoft.com"),
        ("spf.protection.outlook.com",),
        ("selector1", "selector2"),
        ("outlook.com", "hotmail.com", "live.com"),
    ),
    MailProvider("SendGrid", ("smtp.sendgrid.net",), ("sendgrid.net",), ("s1", "s2")),
    MailProvider("Amazon SES", ("amazonaws.com",), ("amazonses.com",), ()),
    MailProvider("Mailgun", ("mailgun.org",), ("mailgun.org",), ("smtp", "mx", "k1")),
    MailProvider("Brevo", ("smtp-relay.brevo.com", "smtp-relay.sendinblue.com"), ("spf.brevo.com", "spf.sendinblue.com"), ("mail",)),
    MailProvider("Zoho", ("zoho.com", "zoho.in", "zoho.eu"), ("zoho.com", "zoho.in", "zoho.eu"), ()),
)


# The message ---------------------------------------------------------------------------------------


def lint_message(message: EmailMessage, *, html_body: str, text_body: str, gate: ContentGate) -> list[Finding]:
    findings: list[Finding] = []
    check = "spam-signals"
    subject = str(message["Subject"] or "")

    if not subject.strip():
        findings.append(error(check, "The subject is empty"))
    if re.match(r"\s*(re|fwd?)\s*:", subject, re.IGNORECASE):
        findings.append(error(check, "The subject starts with RE: or FW:, which filters treat as deceptive"))
    if re.search(r"[!?]{2,}|\${2,}", subject):
        findings.append(error(check, "The subject has repeated !, ? or $ characters"))
    if len(re.findall(r"\b[A-Z]{4,}\b", subject)) >= 2:
        findings.append(warning(check, "The subject has several ALL-CAPS words"))
    if _EMOJI.search(subject):
        findings.append(warning(check, "The subject contains emoji"))
    if len(subject) > 90:
        findings.append(warning(check, f"The subject is long ({len(subject)} characters); keep it under 90"))
    for flag in gate.check_text(subject, "subject"):
        findings.append(error(check, f'The subject contains a filtered term ({flag.category}: "{flag.term}")'))

    for header in ("From", "To", "Date", "Message-ID"):
        if not message[header]:
            findings.append(error(check, f"The {header} header is missing"))
    if len(getaddresses([str(message["To"] or "")])) > 1:
        findings.append(error(check, "More than one address in To: every recipient must get a separate message"))
    from_domain = _domain(parseaddr(str(message["From"] or ""))[1])
    message_id_domain = str(message["Message-ID"] or "").rstrip(">").rpartition("@")[2].lower()
    if from_domain and message_id_domain and message_id_domain != from_domain:
        findings.append(warning(check, f"Message-ID domain ({message_id_domain}) differs from the sender ({from_domain})"))
    reply_domain = _domain(parseaddr(str(message["Reply-To"] or ""))[1])
    if reply_domain and from_domain and reply_domain != from_domain:
        findings.append(warning(check, f"Reply-To domain ({reply_domain}) differs from the sender ({from_domain})"))
    if not message["List-Unsubscribe"]:
        findings.append(warning(check, "No List-Unsubscribe header: people may use 'Report spam' instead"))

    content_types = {part.get_content_type() for part in message.walk()}
    if "text/plain" not in content_types:
        findings.append(error(check, "The message has no plain-text version"))
    visible = text.html_to_text(html_body)
    if len(text_body.strip()) < 0.3 * len(visible):
        findings.append(warning(check, "The plain-text version is much shorter than the HTML version"))
    html_size = len(html_body.encode("utf-8"))
    if html_size > GMAIL_CLIP_BYTES:
        findings.append(warning(check, f"HTML is {html_size / 1000:.0f} KB; Gmail clips above ~102 KB (lower issue.max_stories)"))

    links = [url.strip() for _, url in _HREF.findall(html_body)]
    web_links = [url for url in links if urlsplit(url).scheme.lower() in ("http", "https")]
    if len(web_links) > MAX_LINKS:
        findings.append(warning(check, f"{len(web_links)} links; more than {MAX_LINKS} looks like a link farm"))
    plain_http = [url for url in web_links if url.lower().startswith("http://")]
    if plain_http:
        findings.append(warning(check, f"{len(plain_http)} link(s) use plain http instead of https"))

    images = [src.strip() for _, src in _IMG_SRC.findall(html_body)]
    remote = [src for src in images if not src.lower().startswith("cid:")]
    insecure = [src for src in remote if not src.lower().startswith("https://")]
    if insecure:
        findings.append(error(check, f"{len(insecure)} image(s) are not loaded over https, e.g. {insecure[0][:80]}"))
    if len(remote) > MAX_REMOTE_IMAGES:
        findings.append(warning(check, f"{len(remote)} remote images; fewer images reads less like marketing mail"))
    if remote and len(visible) < 500:
        findings.append(warning(check, "The email is mostly images with little text"))

    for _, href, inner in _ANCHOR.findall(html_body):
        shown = text.html_to_text(inner).strip()
        if _URL_LIKE_TEXT.fullmatch(shown):
            shown_host = _host(shown if "://" in shown else f"https://{shown}")
            if shown_host and shown_host != _host(href):
                findings.append(error(check, f"Link text shows {shown_host} but points to {_host(href) or href[:60]} (looks like phishing)"))

    spam_terms = sorted({flag.term for flag in gate.check_text(visible, "email") if flag.category == "spam_risk"})
    if spam_terms:
        findings.append(warning(check, f"Spam-trigger phrases in the email: {', '.join(spam_terms)}"))
    return list(dict.fromkeys(findings))


# The sender ----------------------------------------------------------------------------------------


@dataclass
class SenderReport:
    address: str
    smtp_host: str
    provider: str | None = None
    spf: list[str] = field(default_factory=list)
    dmarc: list[str] = field(default_factory=list)
    dkim_selectors: list[str] = field(default_factory=list)
    provider_signed: bool = False  # e.g. a gmail.com address sent through Gmail: Google signs it itself
    findings: list[Finding] = field(default_factory=list)


def check_sender(address: str, smtp_host: str, resolver: DnsResolver) -> SenderReport:
    check = "sender-auth"
    report = SenderReport(address=address, smtp_host=smtp_host)
    domain = _domain(address)
    provider = provider_for(smtp_host)
    report.provider = provider.name if provider else None
    if not domain:
        report.findings.append(error(check, f"MAIL_FROM is not a valid address: {address!r}"))
        return report

    try:
        report.spf = [record for record in resolver.txt(domain) if record.lower().startswith("v=spf1")]
        report.dmarc = [record for record in resolver.txt(f"_dmarc.{domain}") if record.lower().startswith("v=dmarc1")]
        if provider and domain in provider.own_domains:
            report.provider_signed = True
            if provider.name == "Google":
                report.findings.append(
                    warning(
                        check,
                        f"Sending from a personal {domain} address is fine for testing, but Microsoft 365 treats it as "
                        "outside mail. Send production issues from a rankuno.com mailbox (planned: Microsoft Graph).",
                    )
                )
            return report  # the provider signs its own domain: SPF, DKIM and DMARC all pass

        report.dkim_selectors = [
            selector for selector in (provider.dkim_selectors if provider else ()) if _has_dkim_key(resolver, selector, domain)
        ]
        spf_ok = bool(provider) and any(_spf_includes(resolver, domain, include) for include in provider.spf_includes)
    except DnsError as exc:
        report.findings.append(warning(check, f"Could not verify the sender's DNS records: {exc}"))
        return report

    if len(report.spf) > 1:
        report.findings.append(error(check, f"{domain} has {len(report.spf)} SPF records; receivers treat that as a failure. Merge them into one."))
    if provider is None:
        report.findings.append(
            warning(check, f"Unrecognised mail server {smtp_host}: confirm {domain}'s SPF record includes it and DKIM signing is on")
        )
    elif not spf_ok and not report.dkim_selectors:
        report.findings.append(
            error(
                check,
                f"{domain} does not authorise {provider.name} ({smtp_host}) to send its mail: its SPF record has no "
                f"include:{provider.spf_includes[0]} and no {provider.name} DKIM key was found. Mail from {address} "
                "would fail SPF and DMARC and land in spam or be rejected.",
            )
        )
    elif not report.dkim_selectors:
        selectors = ", ".join(f"{selector}._domainkey.{domain}" for selector in provider.dkim_selectors) or "the provider's selector"
        report.findings.append(warning(check, f"No {provider.name} DKIM key found ({selectors}). Turn on DKIM signing for {domain}."))
    elif not spf_ok:
        report.findings.append(warning(check, f"{domain}'s SPF record does not include {provider.spf_includes[0]}; add it."))

    if not report.dmarc:
        report.findings.append(
            warning(
                check,
                f"{domain} has no DMARC record. Gmail, Yahoo and Microsoft expect one; add a TXT record at "
                f'_dmarc.{domain} such as "v=DMARC1; p=none; rua=mailto:dmarc-reports@{domain}".',
            )
        )
    return report


def provider_for(smtp_host: str) -> MailProvider | None:
    host = (smtp_host or "").lower().rstrip(".")
    for provider in PROVIDERS:
        if any(host == known or host.endswith("." + known) for known in provider.smtp_hosts):
            return provider
    return None


def _spf_includes(resolver: DnsResolver, domain: str, wanted: str, budget: int = 10) -> bool:
    """Whether the domain's SPF record includes `wanted`, following nested includes (at most 10 lookups, as SPF does)."""
    pending, seen = [domain], set()
    while pending and budget > 0:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        budget -= 1
        for record in resolver.txt(current):
            if not record.lower().startswith("v=spf1"):
                continue
            for term in record.lower().split()[1:]:
                prefix = next((prefix for prefix in _SPF_REFERENCES if term.startswith(prefix)), None)
                if prefix is None:
                    continue
                target = term[len(prefix):].rstrip(".")
                if target == wanted:
                    return True
                pending.append(target)
    return False


def _has_dkim_key(resolver: DnsResolver, selector: str, domain: str) -> bool:
    name = f"{selector}._domainkey.{domain}"
    if any("p=" in record for record in resolver.txt(name)):
        return True
    return bool(resolver.lookup(name, "CNAME"))  # Microsoft 365 publishes DKIM as CNAMEs to its own zone


def _domain(address: str) -> str:
    address = (address or "").strip().lower()
    return address.rpartition("@")[2] if "@" in address else ""


def _host(url: str) -> str:
    try:
        return (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
