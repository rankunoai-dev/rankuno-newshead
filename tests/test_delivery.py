import smtplib
from datetime import timedelta

from conftest import NOW

from rankuno_brief import db, render
from rankuno_brief.mailer import build_message, deliver_issue


class FakeTransport:
    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.sent = []

    def send(self, message):
        if message["To"] in self.fail_for:
            raise smtplib.SMTPRecipientsRefused({message["To"]: (550, b"mailbox unavailable")})
        self.sent.append(message["To"])


def make_issue(conn):
    issue_id = db.save_issue(
        conn,
        issue_date="2026-09-14",
        number=1,
        subject="The RankUno Brief | Monday, 14 September 2026",
        window_start=NOW - timedelta(days=4),
        window_end=NOW,
        html_path="data/issues/x/email.html",
        text_path="data/issues/x/email.txt",
        built_at=NOW,
        stories=[],
    )
    return conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()


def message_for(recipient):
    return build_message(subject="S", html_body="<p>h</p>", text_body="t", sender="brief@rankuno.com",
                         sender_name="The RankUno Brief", recipient=recipient)


def test_rerunning_a_send_never_emails_anyone_twice(conn):
    issue = make_issue(conn)
    transport = FakeTransport()
    first = deliver_issue(conn, issue, ["a@x.com", "b@x.com"], transport, message_for, now=lambda: NOW)
    second = deliver_issue(conn, issue, ["a@x.com", "b@x.com"], transport, message_for, now=lambda: NOW)

    assert first.sent == ["a@x.com", "b@x.com"]
    assert second.sent == [] and second.already_sent == ["a@x.com", "b@x.com"]
    assert transport.sent == ["a@x.com", "b@x.com"]
    assert db.get_issue(conn, "2026-09-14")["status"] == "sent"


def test_failed_recipient_is_retried_on_next_run(conn):
    issue = make_issue(conn)
    first = deliver_issue(conn, issue, ["a@x.com", "b@x.com"], FakeTransport(fail_for={"b@x.com"}), message_for, now=lambda: NOW)
    assert first.failed[0][0] == "b@x.com"
    assert db.get_issue(conn, "2026-09-14")["status"] == "partial"

    retry_transport = FakeTransport()
    second = deliver_issue(conn, issue, ["a@x.com", "b@x.com"], retry_transport, message_for, now=lambda: NOW)
    assert retry_transport.sent == ["b@x.com"]
    assert second.complete
    assert db.get_issue(conn, "2026-09-14")["status"] == "sent"


def test_interrupted_send_is_reported_not_repeated(conn):
    issue = make_issue(conn)
    db.mark_delivery(conn, issue["id"], "a@x.com", "sending", NOW)  # process died mid-send last time
    transport = FakeTransport()
    report = deliver_issue(conn, issue, ["a@x.com"], transport, message_for, now=lambda: NOW)
    assert report.uncertain == ["a@x.com"]
    assert transport.sent == []


def test_sent_issue_cannot_be_rebuilt(conn):
    issue = make_issue(conn)
    deliver_issue(conn, issue, ["a@x.com"], FakeTransport(), message_for, now=lambda: NOW)
    try:
        make_issue(conn)
    except ValueError as exc:
        assert "already been sent" in str(exc)
    else:
        raise AssertionError("rebuilding a sent issue should fail")


def test_message_has_plain_text_and_html_with_embedded_logos(cfg):
    message = build_message(subject="S", html_body='<img src="cid:logo">', text_body="t", sender="brief@rankuno.com",
                            sender_name="The RankUno Brief", recipient="a@x.com", inline_images=render.inline_images(cfg))
    assert message.get_content_type() == "multipart/alternative"
    text_part, html_part = message.iter_parts()
    assert text_part.get_content_type() == "text/plain"
    assert html_part.get_content_type() == "multipart/related"
    embedded = {part["Content-ID"]: part.get_content_type() for part in html_part.iter_parts() if part["Content-ID"]}
    assert embedded == {"<logo>": "image/png", "<logo_white>": "image/png"}
