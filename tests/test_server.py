import base64
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from rankuno_brief import db
from rankuno_brief.server import ScheduleState, authorized, due_jobs, next_slot, preview_page, same_origin

IST = ZoneInfo("Asia/Kolkata")
TOKEN = "k" * 40


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=IST)


# Schedule (settings.yaml: fetch 06:00, Mon and Thu 09:00) ---------------------------------------


def test_daily_fetch_runs_once_after_its_time(cfg):
    state = ScheduleState()
    assert due_jobs(at("2026-09-15T05:59"), cfg, state, set()) == []
    assert due_jobs(at("2026-09-15T06:00"), cfg, state, set()) == ["fetch"]
    state.last_fetch_date = date(2026, 9, 15)
    assert due_jobs(at("2026-09-15T18:00"), cfg, state, set()) == []


def test_issue_runs_at_the_slot_and_only_once(cfg):
    state = ScheduleState(last_fetch_date=date(2026, 9, 17))
    assert due_jobs(at("2026-09-17T08:59"), cfg, state, set()) == []  # Thursday
    assert due_jobs(at("2026-09-17T09:00"), cfg, state, set()) == ["production"]
    state.production_dates.add(date(2026, 9, 17))
    assert due_jobs(at("2026-09-17T09:30"), cfg, state, set()) == []


def test_missed_slot_catches_up_within_three_hours_unless_already_sent(cfg):
    fetched = ScheduleState(last_fetch_date=date(2026, 9, 17))
    assert due_jobs(at("2026-09-17T11:30"), cfg, fetched, set()) == ["production"]
    assert due_jobs(at("2026-09-17T12:30"), cfg, fetched, set()) == []
    assert due_jobs(at("2026-09-17T10:00"), cfg, fetched, {date(2026, 9, 17)}) == []
    assert due_jobs(at("2026-09-16T09:00"), cfg, ScheduleState(last_fetch_date=date(2026, 9, 16)), set()) == []  # Wednesday


def test_next_slot(cfg):
    assert next_slot(at("2026-09-15T12:00"), cfg) == at("2026-09-17T09:00")
    assert next_slot(at("2026-09-17T09:00"), cfg) == at("2026-09-21T09:00")


# Admin page protection --------------------------------------------------------------------------


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_admin_accepts_the_token_as_bearer_or_basic_password():
    assert authorized(f"Bearer {TOKEN}", TOKEN)
    assert authorized(basic("anyone", TOKEN), TOKEN)
    assert not authorized(f"Bearer {TOKEN[:-1]}x", TOKEN)
    assert not authorized(basic(TOKEN, "wrong"), TOKEN)
    assert not authorized("Basic not-base64!!", TOKEN)
    assert not authorized(None, TOKEN)
    assert not authorized(f"Bearer {TOKEN}", "")  # no token configured: admin is off


def test_form_posts_must_come_from_the_admin_page():
    assert same_origin({"Host": "brief.up.railway.app", "Origin": "https://brief.up.railway.app"})
    assert not same_origin({"Host": "brief.up.railway.app", "Origin": "https://evil.example"})
    assert not same_origin({"Host": "brief.up.railway.app", "Origin": "null"})
    assert not same_origin({"Host": "brief.up.railway.app"})


def test_preview_inlines_logos_from_the_data_dir(cfg, tmp_path):
    local = replace(cfg, data_root=tmp_path)
    issue_dir = tmp_path / "issues" / "2026-09-17"
    issue_dir.mkdir(parents=True)
    (issue_dir / "email.html").write_text('<img src="cid:logo"><img src="cid:logo_white">', encoding="utf-8")
    conn = db.connect(local.db_path)
    db.save_issue(conn, issue_date="2026-09-17", number=1, subject="S", window_start=at("2026-09-14T09:00"),
                  window_end=at("2026-09-17T09:00"), html_path="issues/2026-09-17/email.html",
                  text_path="issues/2026-09-17/email.txt", built_at=at("2026-09-17T09:00"), stories=[])
    conn.close()
    page = preview_page(local)
    assert "cid:" not in page and page.count("data:image/png;base64,") == 2
