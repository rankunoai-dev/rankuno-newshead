"""The result type every security check reports in."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

ERROR = "error"  # stops the send
WARNING = "warning"  # reported, does not stop the send


@dataclass(frozen=True)
class Finding:
    level: str
    check: str  # which check raised it, e.g. "content", "recipients", "sender-auth"
    message: str

    @property
    def is_error(self) -> bool:
        return self.level == ERROR


def error(check: str, message: str) -> Finding:
    return Finding(ERROR, check, message)


def warning(check: str, message: str) -> Finding:
    return Finding(WARNING, check, message)


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(finding.is_error for finding in findings)
