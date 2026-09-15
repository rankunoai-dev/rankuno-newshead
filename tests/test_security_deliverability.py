from datetime import date, timedelta

import pytest
from conftest import NOW, make_item
from fake_dns import FakeDns

from rankuno_brief import compose, db, render
from rankuno_brief.mailer import build_message
from rankuno_brief.security import preflight
from rankuno_brief.security.content import gate_for
from rankuno_brief.security.deliverability import check_sender, lint_message
from rankuno_brief.security.findings import has_errors


@pytest.fixture(scope="module")
def issue_bodies(cfg):
    rows = [
        make_item(1, "AI Overviews expand to 40 more countries", "search-engine-land", image="https://example.com/a.jpg",
                  excerpt="Google expanded AI Overviews to more markets and languages this week."),
        make_item(2, "Google Ads adds Performance Max reports", "ppc-land", excerpt="New asset-level reporting."),
    ]
    content = compose.build_content(rows, cfg, NOW)
    meta = render.IssueMeta(number=1, issue_date=date(2026, 9, 14), window_start=NOW - timedelta(days=3),
                            window_end=NOW, subject=render.make_subject(cfg, date(2026, 9, 14)))
    return (meta.subject, *render.render_issue(content, meta, cfg))


def message(cfg, subject, html_body, text_body, **overrides):
    values = dict(subject=subject, html_body=html_body, text_body=text_body, sender="brief@rankuno.com",
                  sender_name="The RankUno Brief", recipient="anna@rankuno.com",
                  inline_images=render.inline_images(cfg), unsubscribe_mailbox="brief@rankuno.com")
    values.update(overrides)
    return build_message(**values)


def messages_of(findings):
    return " | ".join(finding.message for finding in findings)


# Message ----------------------------------------------------------------------------------------


def test_real_issue_has_no_spam_signals(cfg, issue_bodies):
    subject, html_body, text_body = issue_bodies
    findings = lint_message(message(cfg, *issue_bodies), html_body=html_body, text_body=text_body, gate=gate_for(cfg))
    assert findings == [], messages_of(findings)


def test_message_carries_unsubscribe_and_single_recipient_headers(cfg, issue_bodies):
    built = message(cfg, *issue_bodies, reply_to="editor@rankuno.com")
    assert built["List-Unsubscribe"] == "<mailto:brief@rankuno.com?subject=Unsubscribe>"
    assert built["Reply-To"] == "editor@rankuno.com"
    assert built["To"] == "anna@rankuno.com"


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("RE: The RankUno Brief", "RE: or FW:"),
        ("The RankUno Brief!!!", "repeated !"),
        ("Make money fast | The RankUno Brief", "filtered term"),
    ],
)
def test_spammy_subjects_are_errors(cfg, issue_bodies, subject, expected):
    _, html_body, text_body = issue_bodies
    findings = lint_message(message(cfg, subject, html_body, text_body), html_body=html_body, text_body=text_body, gate=gate_for(cfg))
    assert has_errors(findings) and expected in messages_of(findings)


def test_insecure_images_and_disguised_links_are_errors(cfg, issue_bodies):
    subject, html_body, text_body = issue_bodies
    tampered = html_body.replace(
        "</body>",
        '<img src="http://example.com/x.jpg"><a href="https://evil.example/login">www.rankuno.com</a></body>',
    )
    findings = lint_message(message(cfg, subject, tampered, text_body), html_body=tampered, text_body=text_body, gate=gate_for(cfg))
    text = messages_of(findings)
    assert "not loaded over https" in text and "looks like phishing" in text


def test_missing_unsubscribe_header_is_a_warning(cfg, issue_bodies):
    subject, html_body, text_body = issue_bodies
    built = message(cfg, *issue_bodies, unsubscribe_mailbox=None)
    findings = lint_message(built, html_body=html_body, text_body=text_body, gate=gate_for(cfg))
    assert not has_errors(findings) and "List-Unsubscribe" in messages_of(findings)


# Sender authentication --------------------------------------------------------------------------

RANKUNO_DNS = {
    ("rankuno.com", "TXT"): ['"v=spf1 include:spf.protection.outlook.com -all"', '"MS=ms59298181"'],
    ("selector1._domainkey.rankuno.com", "CNAME"): ["selector1-rankuno-com._domainkey.rankunoindia.onmicrosoft.com."],
}


def test_gmail_address_through_gmail_is_aligned():
    report = check_sender("rankuno@gmail.com", "smtp.gmail.com", FakeDns({("gmail.com", "TXT"): ['"v=spf1 redirect=_spf.google.com"']}))
    assert report.provider_signed and not has_errors(report.findings)


def test_rankuno_address_through_gmail_would_land_in_spam():
    report = check_sender("brief@rankuno.com", "smtp.gmail.com", FakeDns(RANKUNO_DNS))
    assert has_errors(report.findings)
    assert "does not authorise Google" in messages_of(report.findings)


def test_rankuno_address_through_microsoft_365_passes_but_needs_dmarc():
    report = check_sender("brief@rankuno.com", "smtp.office365.com", FakeDns(RANKUNO_DNS))
    assert not has_errors(report.findings)
    assert report.dkim_selectors == ["selector1"]
    assert "no DMARC record" in messages_of(report.findings)


def test_nested_spf_includes_are_followed():
    dns = FakeDns({
        ("rankuno.com", "TXT"): ['"v=spf1 include:_spf.rankuno.com -all"'],
        ("_spf.rankuno.com", "TXT"): ['"v=spf1 include:spf.protection.outlook.com ~all"'],
        ("_dmarc.rankuno.com", "TXT"): ['"v=DMARC1; p=quarantine"'],
    })
    report = check_sender("brief@rankuno.com", "smtp.office365.com", dns)
    assert not has_errors(report.findings)
    assert "SPF record does not include" not in messages_of(report.findings)


def test_dns_outage_only_warns():
    report = check_sender("brief@rankuno.com", "smtp.office365.com", FakeDns(offline=True))
    assert report.findings and not has_errors(report.findings)


# Preflight --------------------------------------------------------------------------------------


def saved_issue(conn, subject, html_body, text_body, **hashes):
    issue_id = db.save_issue(
        conn, issue_date="2026-09-14", number=1, subject=subject, window_start=NOW - timedelta(days=3),
        window_end=NOW, html_path="x.html", text_path="x.txt", built_at=NOW, stories=[], **hashes,
    )
    return conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()


def run(cfg, conn, issue, html_body, text_body, recipients=("forselfrajat58615@gmail.com",)):
    return preflight.run_preflight(
        cfg, conn, issue, html_body=html_body, text_body=text_body, requested_recipients=recipients,
        make_message=lambda recipient: message(cfg, issue["subject"], html_body, text_body, recipient=recipient,
                                               sender="rankuno@gmail.com", unsubscribe_mailbox="rankuno@gmail.com"),
        sender="rankuno@gmail.com", smtp_host="smtp.gmail.com",
        resolver=FakeDns({("gmail.com", "MX"): ["5 gmail-smtp-in.l.google.com."]}),
    )


def test_preflight_passes_a_screened_issue(cfg, conn, issue_bodies):
    subject, html_body, text_body = issue_bodies
    issue = saved_issue(conn, subject, html_body, text_body,
                        html_sha256=preflight.sha256(html_body), text_sha256=preflight.sha256(text_body))
    report = run(cfg, conn, issue, html_body, text_body)
    assert not report.blocked, messages_of(report.findings)
    assert report.recipients.accepted == ["forselfrajat58615@gmail.com"]


def test_preflight_blocks_files_edited_after_the_build(cfg, conn, issue_bodies):
    subject, html_body, text_body = issue_bodies
    issue = saved_issue(conn, subject, html_body, text_body,
                        html_sha256=preflight.sha256(html_body), text_sha256=preflight.sha256(text_body))
    report = run(cfg, conn, issue, html_body.replace("AI Overviews", "AI Overview"), text_body)
    assert report.blocked and report.for_check("integrity")


def test_preflight_blocks_issues_built_before_the_security_layer(cfg, conn, issue_bodies):
    issue = saved_issue(conn, *issue_bodies)
    report = run(cfg, conn, issue, issue_bodies[1], issue_bodies[2])
    assert report.blocked and "built before the security layer" in messages_of(report.for_check("integrity"))
