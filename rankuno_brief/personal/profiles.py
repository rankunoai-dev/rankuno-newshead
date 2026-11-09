"""The client form: reading what a manager typed, checking it, and pre-filling blank optional fields.

Rules-only auto-fill (no AI in v1): blank competitors and keywords are pre-filled from the chosen industries'
defaults in config/personal.yaml. Pre-filled values are listed as suggestions; the profile does not become active
until the manager has reviewed them (see store.active_clients).
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from ..security.recipients import normalize_address, split_addresses
from .settings import PersonalSettings

MAX_NAME = 120
MAX_ALIASES = 10
MAX_COMPETITORS = 15
MAX_KEYWORDS = 30
MAX_EXCLUDES = 20
MAX_RECIPIENTS = 50
MAX_LINE = 200
MAX_CUSTOM_INDUSTRY = 80
CUSTOM_PREFIX = "custom:"  # a manager's own industry: the text after the prefix is its name and its keyword

COMPETITORS_FIELD = "competitors"
KEYWORDS_FIELD = "keywords"


def _lines(value: str) -> list[str]:
    cleaned = []
    for raw in value.replace("\r", "").split("\n"):
        line = " ".join(raw.split())
        if line and line not in cleaned:
            cleaned.append(line[:MAX_LINE])
    return cleaned


def _valid_website(value: str) -> str | None:
    """A website must be an http(s) address with a real host. Blank is allowed (returns '')."""
    value = value.strip()
    if not value:
        return ""
    if len(value) > 300 or any(char.isspace() for char in value):
        return None
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or "." not in parts.hostname:
        return None
    return value


def parse_competitors(lines: list[str]) -> list[dict]:
    """One per line: 'Name' or 'Name | website'."""
    result = []
    for line in lines[:MAX_COMPETITORS]:
        name, _, website = line.partition("|")
        result.append({"name": name.strip(), "website": website.strip()})
    return [item for item in result if item["name"]]


def parse_form(
    fields: Mapping[str, list[str]], settings: PersonalSettings, *, is_new: bool
) -> tuple[dict | None, list[str], dict]:
    """Returns (data, errors, raw recipient text). `data` is None when there are errors.

    The recipients are returned as text: they are checked against the allowlist and DNS by the caller, which
    owns the policy and resolver.
    """
    def one(name: str) -> str:
        values = fields.get(name) or [""]
        return values[0].strip()

    errors: list[str] = []
    name = " ".join(one("client_name").split())
    if len(name) < 2:
        errors.append("Enter the client's name (at least 2 characters).")
    elif len(name) > MAX_NAME:
        errors.append(f"The client name is longer than {MAX_NAME} characters.")

    website = _valid_website(one("website"))
    if website is None:
        errors.append("The website must look like https://example.com.")

    industries = [item for item in fields.get("industries", []) if item in settings.industries]
    custom = " ".join(one("custom_industry").split())
    if custom:
        if len(custom) < 2 or len(custom) > MAX_CUSTOM_INDUSTRY:
            errors.append(f"A custom industry must be 2 to {MAX_CUSTOM_INDUSTRY} characters.")
        elif CUSTOM_PREFIX + custom not in industries:
            industries.append(CUSTOM_PREFIX + custom)
    if not industries:
        errors.append("Choose at least one operating industry, or type one in the box.")

    aliases = _lines(one("aliases"))[:MAX_ALIASES]
    competitors = parse_competitors(_lines(one("competitors")))
    keywords = _lines(one("keywords"))[:MAX_KEYWORDS]
    excludes = _lines(one("excludes"))[:MAX_EXCLUDES]
    if len(competitors) > MAX_COMPETITORS:
        errors.append(f"At most {MAX_COMPETITORS} competitors.")

    suggested: list[str] = []
    if is_new:
        # Pre-fill only when the manager left the field blank, and only on first save.
        # Competitors are never pre-filled: they are specific to each client, so the manager names them.
        if not keywords:
            keywords = _industry_values(industries, settings, "keywords_default")[:MAX_KEYWORDS]
            if keywords:
                suggested.append(KEYWORDS_FIELD)

    raw_day = one("send_day") or "0"
    send_day = int(raw_day) if raw_day.isdigit() and int(raw_day) <= 6 else None
    if send_day is None:
        errors.append("Choose the day of the week the feed should arrive.")
    if errors:
        return None, errors, {}
    data = {
        "send_day": send_day,
        "client_name": name,
        "website": website or "",
        "aliases": aliases,
        "industries": industries,
        "competitors": competitors,
        "keywords": keywords,
        "excludes": excludes,
        "suggested": suggested,
    }
    recipients_text = one("recipients")
    return data, [], {"text": recipients_text}


def _industry_values(industries: list[str], settings: PersonalSettings, attribute: str) -> list[str]:
    seen: list[str] = []
    for industry_id in industries:
        if industry_id.startswith(CUSTOM_PREFIX):
            continue  # a custom industry has no suggestions
        for value in getattr(settings.industry(industry_id), attribute):
            if value.lower() not in {item.lower() for item in seen}:
                seen.append(value)
    return seen


def industry_title(industry_id: str, settings: PersonalSettings) -> str:
    if industry_id.startswith(CUSTOM_PREFIX):
        return industry_id[len(CUSTOM_PREFIX):]
    industry = settings.industry(industry_id)
    return industry.title if industry else industry_id


def industry_keywords(industry_id: str, settings: PersonalSettings) -> tuple[str, ...]:
    if industry_id.startswith(CUSTOM_PREFIX):
        return (industry_id[len(CUSTOM_PREFIX):],)
    industry = settings.industry(industry_id)
    return tuple(industry.keywords) if industry else ()


def industry_terms(industries: list[str], settings: PersonalSettings) -> tuple[str, ...]:
    """Keywords of the chosen config industries (generic industry matching). Custom industries are not included:
    their names are strong keywords of the client instead, see custom_keywords."""
    return tuple(dict.fromkeys(
        term for industry_id in industries if not industry_id.startswith(CUSTOM_PREFIX)
        for term in industry_keywords(industry_id, settings)
    ))


def custom_keywords(industries: list[str]) -> tuple[str, ...]:
    """The names a manager typed for their own industries."""
    return tuple(i[len(CUSTOM_PREFIX):] for i in industries if i.startswith(CUSTOM_PREFIX))


def parse_addresses(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Valid addresses (lower case, de-duplicated) and the rejected entries with reasons. Format check only;
    the allowlist and DNS checks happen in the recipients security layer."""
    accepted: list[str] = []
    rejected: list[tuple[str, str]] = []
    for line in text.replace("\r", "").split("\n"):
        if not line.strip():
            continue
        for entry in split_addresses(line):
            address = normalize_address(entry)
            if address is None:
                rejected.append((entry, "not a valid email address"))
            elif address not in accepted:
                accepted.append(address)
    if len(accepted) > MAX_RECIPIENTS:
        rejected.append(("(list)", f"more than {MAX_RECIPIENTS} team members on one client; split the list"))
        accepted = accepted[:MAX_RECIPIENTS]
    return accepted, rejected
