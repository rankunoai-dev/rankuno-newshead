"""Recipient checks: only valid, allowed, reachable and non-suppressed addresses receive an issue.

A rejected address is skipped and reported; it never stops delivery to everyone else.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .dns import DnsError, DnsResolver
from .findings import Finding, error, warning
from .policy import RecipientPolicy

_LOCAL_PART = re.compile(r"^[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_ANGLE_ADDRESS = re.compile(r"<([^<>]*)>")
# Domains people most often mistype, for "did you mean ...?" hints.
_COMMON_DOMAINS = ("gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "live.com")
_BAD_TLDS = frozenset({"con", "cmo", "ocm", "comm", "coom", "vom", "xom"})  # not real TLDs; ".com" mistyped


@dataclass
class RecipientCheck:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (address as written, reason)
    findings: list[Finding] = field(default_factory=list)


def read_recipients_file(path: Path) -> tuple[str, ...]:
    """Addresses from a text file: one or more per line, '#' comments, Outlook 'Name <address>; ...' pastes."""
    if not path.exists():
        return ()
    entries: list[str] = []
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            entries.extend(split_addresses(line))
    return tuple(entries)


def split_addresses(line: str) -> list[str]:
    entries = []
    for chunk in re.split(r"[,;]", line):
        chunk = chunk.strip()
        if not chunk:
            continue
        bracketed = _ANGLE_ADDRESS.findall(chunk)
        if bracketed:
            entries.append(bracketed[-1].strip())
        else:
            entries.extend(chunk.split())
    return entries


def normalize_address(entry: str) -> str | None:
    """The address in lower case if it is a well-formed, header-safe email address, otherwise None."""
    if not entry or len(entry) > 254 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in entry):
        return None  # whitespace and control characters (CR/LF) could inject extra email headers
    address = entry.strip().lower()
    if address.count("@") != 1 or not address.isascii():
        return None
    local, domain = address.split("@")
    if not local or len(local) > 64 or not _LOCAL_PART.match(local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253 or not all(_DOMAIN_LABEL.match(label) for label in labels):
        return None
    if not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}", labels[-1]):
        return None
    return address


def check_recipients(
    entries: Iterable[str],
    policy: RecipientPolicy,
    *,
    suppressed: Iterable[str] = (),
    resolver: DnsResolver | None = None,
) -> RecipientCheck:
    result = RecipientCheck()
    suppressed_set = {address.lower() for address in suppressed}
    seen: set[str] = set()
    candidates: list[tuple[str, str]] = []

    for entry in entries:
        address = normalize_address(entry)
        if address is None:
            result.rejected.append((entry, "not a valid email address"))
            continue
        if address in seen:
            result.findings.append(warning("recipients", f"{address} is listed more than once; it is sent one copy"))
            continue
        seen.add(address)

        domain = address.split("@")[1]
        explicitly_allowed = address in policy.allowed_addresses
        hint = typo_hint(domain, policy)
        if not explicitly_allowed and not (policy.any_domain or domain in policy.allowed_domains):
            reason = f"domain {domain} is not in recipients.allowed_domains"
            result.rejected.append((entry, f"{reason} (did you mean {hint}?)" if hint else reason))
            continue
        if hint and explicitly_allowed:
            result.findings.append(warning("recipients", f"{address}: domain looks like a typo of {hint}"))
        if domain.rsplit(".", 1)[-1] in _BAD_TLDS:
            result.rejected.append((entry, f"domain ending .{domain.rsplit('.', 1)[-1]} looks like a typo"))
            continue
        if address in suppressed_set:
            result.rejected.append(
                (entry, "suppressed after repeated permanent delivery failures (security unsuppress to re-enable)")
            )
            continue
        candidates.append((entry, address))

    reachable = _reachable_domains({address.split("@")[1] for _, address in candidates}, policy, resolver, result)
    for entry, address in candidates:
        domain = address.split("@")[1]
        if reachable.get(domain, True):
            result.accepted.append(address)
        else:
            result.rejected.append((entry, f"domain {domain} has no mail server (every message would bounce)"))

    if len(result.accepted) > policy.max_recipients:
        result.findings.append(
            error(
                "recipients",
                f"{len(result.accepted)} recipients exceeds recipients.max_recipients ({policy.max_recipients}); "
                "nothing will be sent. Check the list, or raise the limit in config/security.yaml.",
            )
        )
    for entry, reason in result.rejected:
        result.findings.append(warning("recipients", f"Skipping {entry!r}: {reason}"))
    if not result.accepted:
        result.findings.append(error("recipients", "No valid recipients: nothing can be sent"))
    return result


def typo_hint(domain: str, policy: RecipientPolicy) -> str | None:
    """A known domain one or two edits away from the given one, e.g. 'rankuno.co' -> 'rankuno.com'."""
    known = [known for known in (*policy.allowed_domains, *_COMMON_DOMAINS) if known != "*"]
    if domain in known:
        return None
    close = [(edit_distance(domain, candidate), candidate) for candidate in known]
    close = [(distance, candidate) for distance, candidate in close if distance <= 2]
    return min(close)[1] if close else None


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein distance (adjacent swaps count as one edit)."""
    previous_previous: list[int] = []
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = char_a != char_b
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                current[j] = min(current[j], previous_previous[j - 2] + 1)
        previous_previous, previous = previous, current
    return previous[len(b)]


def _reachable_domains(
    domains: set[str], policy: RecipientPolicy, resolver: DnsResolver | None, result: RecipientCheck
) -> dict[str, bool]:
    if not policy.check_mx or resolver is None:
        return {}
    reachable = {}
    for domain in sorted(domains):
        try:
            reachable[domain] = resolver.accepts_mail(domain)
        except DnsError as exc:
            result.findings.append(warning("recipients", f"Could not check the mail server for {domain}: {exc}"))
    return reachable
