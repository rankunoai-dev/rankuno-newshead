"""Send slots ("Mon 09:00") and working out which issue date a build or send belongs to."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_SLOT_PATTERN = re.compile(r"^\s*(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+(\d{1,2}):(\d{2})\s*$", re.IGNORECASE)

# A build shortly after a slot's time still belongs to that slot, e.g. a late scheduler run at 09:40.
SLOT_GRACE = timedelta(hours=12)


@dataclass(frozen=True)
class SendSlot:
    weekday: int
    hour: int
    minute: int


def parse_slot(text: str) -> SendSlot:
    match = _SLOT_PATTERN.match(text)
    if not match:
        raise ValueError(f"Send slot must look like 'Mon 09:00', got {text!r}")
    weekday, hour, minute = _WEEKDAYS[match.group(1).lower()[:3]], int(match.group(2)), int(match.group(3))
    if hour > 23 or minute > 59:
        raise ValueError(f"Send slot has an invalid time: {text!r}")
    return SendSlot(weekday, hour, minute)


def resolve_issue_date(now: datetime, send_slots: Iterable[SendSlot], sent_dates: Collection[date]) -> date:
    """Return the issue date for a build happening at `now` (timezone-aware, local time).

    A slot that passed within SLOT_GRACE and has not been sent yet is still current;
    otherwise the next upcoming slot is used.
    """
    occurrences = sorted(_occurrences(now, tuple(send_slots)))
    past = [slot_time for slot_time in occurrences if slot_time <= now]
    if past and now - past[-1] <= SLOT_GRACE and past[-1].date() not in sent_dates:
        return past[-1].date()
    return next(slot_time for slot_time in occurrences if slot_time > now).date()


def _occurrences(now: datetime, send_slots: tuple[SendSlot, ...]) -> Iterable[datetime]:
    for offset in range(-7, 8):
        day = now.date() + timedelta(days=offset)
        for slot in send_slots:
            if day.weekday() == slot.weekday:
                yield datetime.combine(day, time(slot.hour, slot.minute), tzinfo=now.tzinfo)
