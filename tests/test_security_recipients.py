from dataclasses import replace

import pytest
from fake_dns import FakeDns

from rankuno_brief.security.findings import has_errors
from rankuno_brief.security.policy import RecipientPolicy
from rankuno_brief.security.recipients import (
    check_recipients,
    edit_distance,
    normalize_address,
    read_recipients_file,
)

POLICY = RecipientPolicy(
    allowed_domains=("rankuno.com",),
    allowed_addresses=("tester@gmail.com",),
    max_recipients=5,
    check_mx=True,
    suppress_after_hard_failures=2,
)
MAIL_OK = FakeDns({("rankuno.com", "MX"): ["0 rankuno-com.mail.protection.outlook.com."],
                   ("gmail.com", "MX"): ["5 gmail-smtp-in.l.google.com."]})


def test_recipients_file_accepts_lines_comments_and_outlook_pastes(tmp_path):
    path = tmp_path / "recipients.txt"
    path.write_text(
        "﻿# staff\n"
        "anna@rankuno.com\n"
        "Jane Doe <jane.doe@rankuno.com>; John Roe <john.roe@rankuno.com>\n"
        "ravi@rankuno.com, meera@rankuno.com   # marketing team\n",
        encoding="utf-8",
    )
    assert read_recipients_file(path) == (
        "anna@rankuno.com", "jane.doe@rankuno.com", "john.roe@rankuno.com", "ravi@rankuno.com", "meera@rankuno.com",
    )
    assert read_recipients_file(tmp_path / "missing.txt") == ()


@pytest.mark.parametrize(
    "entry",
    [
        "not-an-address",
        "anna@rankuno",
        "anna@@rankuno.com",
        "an na@rankuno.com",
        "anna@rankuno.com\r\nBcc: everyone@example.com",  # header injection
        "anna@-rankuno.com",
        "ánna@rankuno.com",
        "anna.@rankuno.com",
    ],
)
def test_malformed_or_header_injecting_addresses_are_rejected(entry):
    assert normalize_address(entry) is None


def test_only_allowed_domains_and_listed_addresses_are_accepted():
    result = check_recipients(
        ["Anna@RankUno.com", "tester@gmail.com", "someone@gmail.com", "boss@rankuno.co"], POLICY, resolver=MAIL_OK
    )
    assert result.accepted == ["anna@rankuno.com", "tester@gmail.com"]
    reasons = dict(result.rejected)
    assert "not in recipients.allowed_domains" in reasons["someone@gmail.com"]
    assert "did you mean rankuno.com?" in reasons["boss@rankuno.co"]
    assert not has_errors(result.findings)  # skipped addresses never stop delivery to everyone else


def test_wildcard_domain_still_rejects_mistyped_com():
    open_policy = replace(POLICY, allowed_domains=("*",))
    result = check_recipients(["client@acme.con", "client@acme.com"], open_policy, resolver=None)
    assert result.accepted == ["client@acme.com"]
    assert "looks like a typo" in dict(result.rejected)["client@acme.con"]


def test_duplicates_receive_one_copy():
    result = check_recipients(["anna@rankuno.com", "ANNA@rankuno.com"], POLICY, resolver=MAIL_OK)
    assert result.accepted == ["anna@rankuno.com"]
    assert any("more than once" in finding.message for finding in result.findings)


def test_suppressed_address_is_skipped():
    result = check_recipients(["anna@rankuno.com", "gone@rankuno.com"], POLICY, suppressed={"gone@rankuno.com"}, resolver=MAIL_OK)
    assert result.accepted == ["anna@rankuno.com"]
    assert "suppressed" in dict(result.rejected)["gone@rankuno.com"]


def test_domain_without_a_mail_server_is_skipped():
    policy = replace(POLICY, allowed_domains=("rankuno.com", "old-rankuno.com", "nomail.com"))
    dns = FakeDns({("rankuno.com", "MX"): ["0 mx.rankuno.com."], ("nomail.com", "MX"): ["0 ."]})
    result = check_recipients(["a@rankuno.com", "b@old-rankuno.com", "c@nomail.com"], policy, resolver=dns)
    assert result.accepted == ["a@rankuno.com"]
    assert set(dict(result.rejected)) == {"b@old-rankuno.com", "c@nomail.com"}


def test_dns_outage_warns_but_does_not_stop_the_send():
    result = check_recipients(["anna@rankuno.com"], POLICY, resolver=FakeDns(offline=True))
    assert result.accepted == ["anna@rankuno.com"]
    assert any("Could not check the mail server" in finding.message for finding in result.findings)
    assert not has_errors(result.findings)


def test_too_many_recipients_or_none_at_all_stops_the_send():
    many = [f"person{i}@rankuno.com" for i in range(6)]
    assert has_errors(check_recipients(many, POLICY, resolver=MAIL_OK).findings)
    assert has_errors(check_recipients(["nobody@example.org"], POLICY, resolver=MAIL_OK).findings)


def test_edit_distance_counts_a_swap_as_one_edit():
    assert edit_distance("rankuno.com", "rankuno.com") == 0
    assert edit_distance("rnakuno.com", "rankuno.com") == 1
    assert edit_distance("gmial.com", "gmail.com") == 1
