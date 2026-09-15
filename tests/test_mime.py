"""The email must survive Outlook and Exchange intact: CRLF line endings on every path it leaves by."""

import base64
import email
import re
import smtplib
from datetime import date, timedelta
from email import policy

import httpx
import pytest
from conftest import NOW, make_item

from rankuno_brief import compose, render
from rankuno_brief.mailer import GraphSettings, GraphTransport, SmtpSettings, SmtpTransport, build_message, mime_bytes

BARE_LF = re.compile(rb"(?<!\r)\n")


@pytest.fixture(scope="module")
def issue(cfg):
    rows = [
        make_item(1, "Google AI Overviews Practice Knowledge Quiz", "search-engine-roundtable",
                  excerpt="Did you know that Google AI Overviews can also quiz you on various knowledge topics? " * 3,
                  image="https://www.seroundtable.com/images/ai-overviews-quiz-1757900000.jpg"),
        make_item(2, "Google Search Console Indexing Report Missing Old June Data", "search-engine-roundtable",
                  excerpt="The Google Search Console page indexing report is now missing a huge chunk of data. " * 3),
    ]
    content = compose.build_content(rows, cfg, NOW)
    meta = render.IssueMeta(number=1, issue_date=date(2026, 9, 17), window_start=NOW - timedelta(days=4),
                            window_end=NOW, subject=render.make_subject(cfg, date(2026, 9, 17)))
    html_body, text_body = render.render_issue(content, meta, cfg)
    message = build_message(subject=meta.subject, html_body=html_body, text_body=text_body, sender="brief@rankuno.com",
                            sender_name="The RankUno Brief", recipient="rajat.singh@rankuno.com",
                            inline_images=render.inline_images(cfg), unsubscribe_mailbox="brief@rankuno.com")
    return message, html_body


def html_as_received(raw: bytes) -> str:
    parsed = email.message_from_bytes(raw, policy=policy.default)
    return next(part for part in parsed.walk() if part.get_content_type() == "text/html").get_content()


def assert_intact(raw: bytes, html_body: str) -> None:
    assert BARE_LF.search(raw) is None, "bare LF line endings: Outlook would garble the email"
    assert b"=\n" not in raw  # quoted-printable line wraps are always "=\r\n"
    # set_content ends the body with a newline; otherwise every character must arrive unchanged
    assert html_as_received(raw).replace("\r\n", "\n").rstrip("\n") == html_body.rstrip("\n")


def test_default_serialisation_is_what_broke_outlook(issue):
    message, _ = issue
    assert BARE_LF.search(message.as_bytes())  # why mime_bytes exists


def test_wire_bytes_use_crlf_and_round_trip(issue):
    message, html_body = issue
    assert_intact(mime_bytes(message), html_body)


def test_graph_sends_crlf_mime(issue):
    message, html_body = issue
    bodies = []

    def handler(request):
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        bodies.append(base64.b64decode(request.content))
        return httpx.Response(202)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with GraphTransport(GraphSettings("tenant", "client", "secret"), "brief@rankuno.com", client=client) as transport:
        transport.send(message)
    assert_intact(bodies[0], html_body)


class CapturingSMTP(smtplib.SMTP):
    def __init__(self):
        super().__init__()  # no host: nothing connects
        self.captured = []

    def ehlo_or_helo_if_needed(self):
        pass

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        self.captured.append(msg)
        return {}


def test_smtp_sends_crlf_mime(issue):
    message, html_body = issue
    transport = SmtpTransport(SmtpSettings("smtp.example.com", 587, "u", "p"))
    transport._smtp = CapturingSMTP()
    transport.send(message)
    assert_intact(transport._smtp.captured[0], html_body)
