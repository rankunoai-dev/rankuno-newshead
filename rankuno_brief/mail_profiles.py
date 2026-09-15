"""Mail profiles: how and to whom the production brief and test copies are sent, set entirely by environment variables.

Every setting can be given per profile with a PROD_ or TEST_ prefix. The same name without a prefix is a shared
default used by both profiles, so a value only needs repeating when the two profiles differ:

    MAIL_PROVIDER       smtp | graph                               PROD_MAIL_PROVIDER, TEST_MAIL_PROVIDER
    MAIL_FROM           sending address (the mailbox for graph)    PROD_MAIL_FROM, TEST_MAIL_FROM
    MAIL_FROM_NAME      display name                               ...
    MAIL_REPLY_TO       optional reply address
    SMTP_HOST / SMTP_PORT / SMTP_USERNAME / SMTP_PASSWORD           (smtp)
    GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET         (graph)

Recipients never fall back from one profile to the other:

    TEST_RECIPIENTS     required for test copies; they go to these addresses and nobody else
    TEST_SUBJECT_PREFIX optional subject prefix for test copies, e.g. [TEST]; empty by default
    PROD_RECIPIENTS     optional; when empty the production list is config/recipients.txt
    PROD_SEND_ENABLED   production issues are sent only when this is "true" (test copies always work)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .config import Config, ConfigError
from .mailer import GraphSettings, GraphTransport, SmtpSettings, SmtpTransport
from .security.recipients import split_addresses

PRODUCTION, TEST = "production", "test"
SMTP, GRAPH = "smtp", "graph"
PREFIXES = {PRODUCTION: "PROD_", TEST: "TEST_"}
GRAPH_HOST = "graph.microsoft.com"
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class MailProfile:
    name: str  # production | test
    provider: str  # smtp | graph
    sender: str
    sender_name: str
    reply_to: str | None
    recipients: tuple[str, ...]
    recipients_source: str
    send_enabled: bool
    smtp: SmtpSettings | None = None
    graph: GraphSettings | None = None
    subject_prefix: str = ""  # TEST_SUBJECT_PREFIX, e.g. "[TEST] "; production subjects never get one

    @property
    def is_test(self) -> bool:
        return self.name == TEST

    @property
    def server_host(self) -> str:
        """The host that actually sends, for the SPF/DKIM/DMARC check."""
        return self.smtp.host if self.smtp else GRAPH_HOST

    def subject(self, subject: str) -> str:
        return f"{self.subject_prefix}{subject}" if self.subject_prefix else subject

    def transport(self):
        if self.provider == GRAPH:
            return GraphTransport(self.graph, self.sender)
        return SmtpTransport(self.smtp)

    def describe(self) -> list[str]:
        """Human-readable settings with every secret hidden."""
        lines = [f"Profile:     {self.name}", f"Provider:    {self.provider}", f"From:        {_from(self)}"]
        if self.reply_to:
            lines.append(f"Reply-To:    {self.reply_to}")
        if self.smtp:
            lines.append(f"SMTP:        {self.smtp.username} @ {self.smtp.host}:{self.smtp.port} (password {_mask(self.smtp.password)})")
        if self.graph:
            lines.append(
                f"Graph:       tenant {self.graph.tenant_id}, app {self.graph.client_id} (secret {_mask(self.graph.client_secret)})"
            )
        lines.append(f"Recipients:  {len(self.recipients)} from {self.recipients_source}")
        if self.name == PRODUCTION:
            lines.append(f"Sending:     {'ON' if self.send_enabled else 'OFF (set PROD_SEND_ENABLED=true to send production issues)'}")
        return lines


def load_profile(name: str, cfg: Config, environ: Mapping[str, str] | None = None) -> MailProfile:
    env = os.environ if environ is None else environ
    prefix = PREFIXES[name]
    problems: list[str] = []

    def setting(key: str, default: str = "") -> str:
        return (env.get(prefix + key) or env.get(key) or default).strip()

    def required(key: str) -> str:
        value = setting(key)
        if not value:
            problems.append(f"{prefix}{key} (or {key})")
        return value

    provider = setting("MAIL_PROVIDER", SMTP).lower()
    if provider not in (SMTP, GRAPH):
        problems.append(f"{prefix}MAIL_PROVIDER must be 'smtp' or 'graph', not {provider!r}")
    sender = required("MAIL_FROM")

    smtp = graph = None
    if provider == SMTP:
        host, username, password = required("SMTP_HOST"), required("SMTP_USERNAME"), required("SMTP_PASSWORD")
        port_text = setting("SMTP_PORT", "587")
        if not port_text.isdigit():
            problems.append(f"{prefix}SMTP_PORT must be a number, not {port_text!r}")
        smtp = SmtpSettings(host=host, port=int(port_text) if port_text.isdigit() else 587, username=username, password=password)
    elif provider == GRAPH:
        graph = GraphSettings(
            tenant_id=required("GRAPH_TENANT_ID"),
            client_id=required("GRAPH_CLIENT_ID"),
            client_secret=required("GRAPH_CLIENT_SECRET"),
        )

    if name == TEST:
        recipients, source = tuple(split_addresses(env.get("TEST_RECIPIENTS", ""))), "TEST_RECIPIENTS"
        if not recipients:
            problems.append("TEST_RECIPIENTS (test copies go only to the addresses listed there)")
    elif env.get("PROD_RECIPIENTS", "").strip():
        recipients, source = tuple(split_addresses(env["PROD_RECIPIENTS"])), "PROD_RECIPIENTS"
    else:
        file = cfg.delivery.recipients_file
        recipients = cfg.delivery.recipients
        source = str(file.relative_to(cfg.root)) if file and file.is_relative_to(cfg.root) else str(file or "settings.yaml")

    if problems:
        raise ConfigError(f"Mail settings for the {name} profile are incomplete. Set: " + "; ".join(problems))
    return MailProfile(
        name=name,
        provider=provider,
        sender=sender,
        sender_name=setting("MAIL_FROM_NAME", cfg.newsletter.name),
        reply_to=setting("MAIL_REPLY_TO") or cfg.security.sending.reply_to or None,
        recipients=recipients,
        recipients_source=source,
        send_enabled=name == TEST or production_sending_enabled(env),
        smtp=smtp,
        graph=graph,
        subject_prefix=_prefix(env.get("TEST_SUBJECT_PREFIX", "")) if name == TEST else "",
    )


def _prefix(value: str) -> str:
    value = value.strip()
    return f"{value} " if value else ""


def production_sending_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get("PROD_SEND_ENABLED", "").strip().lower() in _TRUE


def _from(profile: MailProfile) -> str:
    return f"{profile.sender_name} <{profile.sender}>" if profile.sender_name else profile.sender


def _mask(secret: str) -> str:
    return "not set" if not secret else f"set, {len(secret)} characters"
