from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from rankuno_brief.slots import parse_slot, resolve_issue_date

IST = ZoneInfo("Asia/Kolkata")
SLOTS = (parse_slot("Mon 09:00"), parse_slot("Thu 09:00"))


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)  # 14 Sep 2026 is a Monday


def test_parse_slot_rejects_bad_input():
    assert parse_slot("thursday 9:30").weekday == 3
    with pytest.raises(ValueError):
        parse_slot("Someday 09:00")
    with pytest.raises(ValueError):
        parse_slot("Mon 25:00")


def test_weekend_build_targets_monday():
    assert resolve_issue_date(at(13, 20), SLOTS, set()) == date(2026, 9, 14)


def test_build_just_before_slot_targets_that_slot():
    assert resolve_issue_date(at(14, 8, 45), SLOTS, set()) == date(2026, 9, 14)


def test_late_run_after_slot_still_targets_unsent_slot():
    assert resolve_issue_date(at(14, 9, 40), SLOTS, set()) == date(2026, 9, 14)


def test_after_monday_is_sent_next_build_targets_thursday():
    assert resolve_issue_date(at(14, 9, 40), SLOTS, {date(2026, 9, 14)}) == date(2026, 9, 17)


def test_long_after_slot_moves_to_next_slot():
    assert resolve_issue_date(at(15, 12), SLOTS, set()) == date(2026, 9, 17)
