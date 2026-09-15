import base64
import email
import smtplib
from email import policy

import httpx
import pytest

from rankuno_brief.config import ConfigError
from rankuno_brief.mail_profiles import GRAPH, PRODUCTION, SMTP, TEST, load_profile
from rankuno_brief.mailer import AuthError, GraphSettings, GraphTransport, MailError, RecipientRejected, SmtpSettings, SmtpTransport, build_message

SHARED_GMAIL = {
    "MAIL_FROM": "rankuno@gmail.com",
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_USERNAME": "rankuno@gmail.com",
    "SMTP_PASSWORD": "app-password-1234",
    "TEST_RECIPIENTS": "rajat.singh@rankuno.com",
}


# Profiles ---------------------------------------------------------------------------------------


def test_shared_settings_serve_both_profiles_but_recipients_never_mix(cfg):
    production, test = load_profile(PRODUCTION, cfg, SHARED_GMAIL), load_profile(TEST, cfg, SHARED_GMAIL)
    assert production.provider == test.provider == SMTP
    assert production.sender == test.sender == "rankuno@gmail.com"
    assert test.recipients == ("rajat.singh@rankuno.com",) and test.recipients_source == "TEST_RECIPIENTS"
    assert production.recipients == cfg.delivery.recipients and "recipients.txt" in production.recipients_source


def test_prefixed_settings_override_shared_ones_per_profile(cfg):
    env = {
        **SHARED_GMAIL,
        "PROD_MAIL_PROVIDER": "graph",
        "PROD_MAIL_FROM": "brief@rankuno.com",
        "PROD_GRAPH_TENANT_ID": "tenant",
        "PROD_GRAPH_CLIENT_ID": "client",
        "PROD_GRAPH_CLIENT_SECRET": "secret-value",
        "PROD_RECIPIENTS": "Anna <anna@rankuno.com>; ravi@rankuno.com",
        "TEST_MAIL_FROM_NAME": "Brief Test",
    }
    production, test = load_profile(PRODUCTION, cfg, env), load_profile(TEST, cfg, env)
    assert (production.provider, production.sender, production.server_host) == (GRAPH, "brief@rankuno.com", "graph.microsoft.com")
    assert production.recipients == ("anna@rankuno.com", "ravi@rankuno.com")
    assert (test.provider, test.sender, test.sender_name) == (SMTP, "rankuno@gmail.com", "Brief Test")


def test_test_copies_require_test_recipients(cfg):
    env = {key: value for key, value in SHARED_GMAIL.items() if key != "TEST_RECIPIENTS"}
    with pytest.raises(ConfigError, match="TEST_RECIPIENTS"):
        load_profile(TEST, cfg, env)


def test_production_sending_is_off_unless_switched_on(cfg):
    assert not load_profile(PRODUCTION, cfg, SHARED_GMAIL).send_enabled
    assert load_profile(PRODUCTION, cfg, {**SHARED_GMAIL, "PROD_SEND_ENABLED": "true"}).send_enabled
    assert load_profile(TEST, cfg, SHARED_GMAIL).send_enabled


def test_missing_settings_are_named(cfg):
    with pytest.raises(ConfigError, match="PROD_GRAPH_CLIENT_SECRET"):
        load_profile(PRODUCTION, cfg, {"MAIL_PROVIDER": "graph", "MAIL_FROM": "brief@rankuno.com"})


def test_description_never_shows_secrets(cfg):
    env = {**SHARED_GMAIL, "MAIL_PROVIDER": "graph", "GRAPH_TENANT_ID": "t", "GRAPH_CLIENT_ID": "c", "GRAPH_CLIENT_SECRET": "top-secret-value"}
    text = "\n".join(load_profile(TEST, cfg, env).describe()) + "\n".join(load_profile(TEST, cfg, SHARED_GMAIL).describe())
    assert "top-secret-value" not in text and "app-password-1234" not in text


# SMTP -------------------------------------------------------------------------------------------


class RefusingSMTP:
    def __init__(self, error):
        self.error = error

    def send_message(self, message):
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected", "permanent"),
    [
        (smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"no such user")}), RecipientRejected, True),
        (smtplib.SMTPRecipientsRefused({"a@x.com": (452, b"mailbox full")}), RecipientRejected, False),
        (smtplib.SMTPDataError(550, b"5.7.1 rejected as spam"), MailError, None),
        (smtplib.SMTPServerDisconnected("gone"), MailError, None),
    ],
)
def test_smtp_errors_become_mail_errors(error, expected, permanent):
    transport = SmtpTransport(SmtpSettings("smtp.example.com", 587, "u", "p"))
    transport._smtp = RefusingSMTP(error)
    with pytest.raises(expected) as raised:
        transport.send(build_message(subject="S", html_body="<p>h</p>", text_body="t", sender="a@b.com", sender_name="", recipient="a@x.com"))
    if permanent is not None:
        assert raised.value.permanent is permanent


# Microsoft Graph --------------------------------------------------------------------------------


def message():
    return build_message(subject="The RankUno Brief", html_body="<p>Hello</p>", text_body="Hello",
                         sender="brief@rankuno.com", sender_name="The RankUno Brief", recipient="rajat.singh@rankuno.com")


def graph(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GraphTransport(GraphSettings("tenant-id", "client-id", "client-secret"), "brief@rankuno.com",
                          client=client, sleep=lambda seconds: None, **kwargs)


def token_response():
    return httpx.Response(200, json={"access_token": "token-1", "expires_in": 3599})


def test_graph_sends_the_mime_message_as_the_mailbox():
    calls = []

    def handler(request):
        calls.append(request)
        return token_response() if "login.microsoftonline.com" in str(request.url) else httpx.Response(202)

    with graph(handler) as transport:
        transport.send(message())

    token_call, send_call = calls
    assert str(token_call.url) == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
    assert b"grant_type=client_credentials" in token_call.content
    assert str(send_call.url) == "https://graph.microsoft.com/v1.0/users/brief@rankuno.com/sendMail"
    assert send_call.headers["Authorization"] == "Bearer token-1"
    assert send_call.headers["Content-Type"] == "text/plain"
    sent = email.message_from_bytes(base64.b64decode(send_call.content), policy=policy.default)
    assert sent["To"] == "rajat.singh@rankuno.com" and sent["Subject"] == "The RankUno Brief"


def test_graph_bad_credentials_fail_before_sending():
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret"})

    with pytest.raises(AuthError, match="GRAPH_CLIENT_SECRET"):
        graph(handler).__enter__()


def test_graph_throttling_is_retried():
    responses = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(202)])

    def handler(request):
        return token_response() if "login" in str(request.url) else next(responses)

    with graph(handler) as transport:
        transport.send(message())


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (403, "ErrorAccessDenied", AuthError),
        (404, "ErrorInvalidUser", AuthError),
        (400, "ErrorInvalidRecipients", RecipientRejected),
        (400, "ErrorMimeContentInvalidBase64String", MailError),
    ],
)
def test_graph_refusals_are_explained(status, code, expected):
    def handler(request):
        if "login" in str(request.url):
            return token_response()
        return httpx.Response(status, json={"error": {"code": code, "message": "refused"}})

    with graph(handler) as transport, pytest.raises(expected):
        transport.send(message())
