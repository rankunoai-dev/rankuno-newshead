"""DNS lookups over HTTPS (Cloudflare, then Google), so no extra dependency or system resolver is needed.

Only public DNS names (mail domains) are ever sent. Results are cached for the life of the resolver.
"""

from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

ENDPOINTS = ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
RECORD_TYPES = {"A": 1, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28}
_NOERROR, _NXDOMAIN = 0, 3
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


class DnsError(Exception):
    """The lookup could not be completed (network problem or resolver failure), as opposed to 'no record'."""


class DnsResolver:
    def __init__(self, client: httpx.Client | None = None, timeout: float = 8.0) -> None:
        self._client = client or httpx.Client(timeout=timeout, headers={"Accept": "application/dns-json"})
        self._cache: dict[tuple[str, str], list[str]] = {}

    def lookup(self, name: str, record_type: str) -> list[str]:
        """Record data for the name, or [] when the name or record does not exist. Raises DnsError."""
        key = (name.lower().rstrip("."), record_type)
        if key not in self._cache:
            self._cache[key] = self._query(*key)
        return self._cache[key]

    def txt(self, name: str) -> list[str]:
        return [_join_txt(value) for value in self.lookup(name, "TXT")]

    def accepts_mail(self, domain: str) -> bool:
        records = self.lookup(domain, "MX")
        if records:
            # A lone "0 ." (null MX, RFC 7505) means the domain accepts no mail at all.
            return any(value.split()[-1].rstrip(".") for value in records if value.split())
        # RFC 5321: without an MX record, mail goes to the domain's own address.
        return bool(self.lookup(domain, "A") or self.lookup(domain, "AAAA"))

    def _query(self, name: str, record_type: str) -> list[str]:
        problems = []
        for endpoint in ENDPOINTS:
            try:
                response = self._client.get(endpoint, params={"name": name, "type": record_type})
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                problems.append(f"{endpoint}: {type(exc).__name__}")
                continue
            status = payload.get("Status")
            if status == _NXDOMAIN:
                return []
            if status != _NOERROR:
                problems.append(f"{endpoint}: DNS status {status}")
                continue
            wanted = RECORD_TYPES[record_type]
            return [answer["data"] for answer in payload.get("Answer") or () if answer.get("type") == wanted]
        raise DnsError(f"DNS lookup for {name} ({record_type}) failed: {'; '.join(problems)}")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DnsResolver:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _join_txt(value: str) -> str:
    """TXT data arrives as one or more quoted strings: '"v=spf1 include:a" " -all"' -> 'v=spf1 include:a -all'."""
    chunks = _QUOTED.findall(value)
    return "".join(chunks) if chunks else value
