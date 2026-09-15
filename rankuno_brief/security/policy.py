"""Security settings from config/security.yaml and config/content_filter.yaml.

Parsing raises KeyError, TypeError or ValueError; config.load_config turns those into ConfigError.
"""

from __future__ import annotations

from dataclasses import dataclass

BLOCK = "block"
HOLD = "hold"
ANY_DOMAIN = "*"


@dataclass(frozen=True)
class FilterCategory:
    id: str
    action: str  # block | hold
    terms: tuple[str, ...]


@dataclass(frozen=True)
class ContentPolicy:
    categories: tuple[FilterCategory, ...]
    allow_phrases: tuple[str, ...]
    blocked_domains: tuple[str, ...]
    url_shorteners: tuple[str, ...]
    community_images: bool


@dataclass(frozen=True)
class RecipientPolicy:
    allowed_domains: tuple[str, ...]
    allowed_addresses: tuple[str, ...]
    max_recipients: int
    check_mx: bool
    suppress_after_hard_failures: int

    @property
    def any_domain(self) -> bool:
        return ANY_DOMAIN in self.allowed_domains


@dataclass(frozen=True)
class SendingPolicy:
    delay_seconds: float
    reply_to: str
    list_unsubscribe: bool
    unsubscribe_mailbox: str


@dataclass(frozen=True)
class SecurityPolicy:
    content: ContentPolicy
    recipients: RecipientPolicy
    sending: SendingPolicy


def parse_policy(security_doc: dict, filter_doc: dict) -> SecurityPolicy:
    content_doc = security_doc.get("content") or {}
    recipients_doc = security_doc["recipients"]
    sending_doc = security_doc.get("sending") or {}

    categories = []
    for entry in filter_doc["categories"]:
        action = entry["action"]
        if action not in (BLOCK, HOLD):
            raise ValueError(f"content_filter.yaml category '{entry['id']}' has unknown action '{action}'")
        terms = tuple(str(term).strip() for term in entry.get("terms") or () if str(term).strip())
        categories.append(FilterCategory(id=entry["id"], action=action, terms=terms))
    ids = [category.id for category in categories]
    if len(ids) != len(set(ids)):
        raise ValueError("content_filter.yaml has duplicate category ids")

    recipients = RecipientPolicy(
        allowed_domains=_lower_tuple(recipients_doc.get("allowed_domains")),
        allowed_addresses=_lower_tuple(recipients_doc.get("allowed_addresses")),
        max_recipients=int(recipients_doc.get("max_recipients", 450)),
        check_mx=bool(recipients_doc.get("check_mx", True)),
        suppress_after_hard_failures=int(recipients_doc.get("suppress_after_hard_failures", 2)),
    )
    if recipients.max_recipients < 1 or recipients.suppress_after_hard_failures < 1:
        raise ValueError("security.yaml recipients.max_recipients and suppress_after_hard_failures must be at least 1")

    return SecurityPolicy(
        content=ContentPolicy(
            categories=tuple(categories),
            allow_phrases=tuple(str(phrase) for phrase in filter_doc.get("allow_phrases") or ()),
            blocked_domains=_lower_tuple(filter_doc.get("blocked_domains")),
            url_shorteners=_lower_tuple(filter_doc.get("url_shorteners")),
            community_images=bool(content_doc.get("community_images", False)),
        ),
        recipients=recipients,
        sending=SendingPolicy(
            delay_seconds=max(0.0, float(sending_doc.get("delay_seconds", 2.5))),
            reply_to=str(sending_doc.get("reply_to") or "").strip(),
            list_unsubscribe=bool(sending_doc.get("list_unsubscribe", True)),
            unsubscribe_mailbox=str(sending_doc.get("unsubscribe_mailbox") or "").strip(),
        ),
    )


def _lower_tuple(values) -> tuple[str, ...]:
    return tuple(str(value).strip().lower() for value in values or () if str(value).strip())
