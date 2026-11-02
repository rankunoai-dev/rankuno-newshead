from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from rankuno_brief import db
from rankuno_brief.personal import feed, mail, matching, profiles, store, web
from rankuno_brief.personal.settings import cached_settings
from fake_dns import FakeDns

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
REAL = datetime.now(timezone.utc)  # web sessions are checked against the real clock

IST = ZoneInfo("Asia/Kolkata")
MX = {("rankuno.com", "MX"): ["10 mail.rankuno.com."]}


@pytest.fixture
def settings(cfg):
    return cached_settings(cfg.root)


@pytest.fixture
def pconn(conn):
    conn.executescript(store.SCHEMA)
    return conn


def add_item(conn, item_id, title, *, excerpt="", published=None, source="sources-test", publisher=None):
    conn.execute(
        """INSERT INTO items (id, url_hash, source_id, title, url, publisher, published_at, fetched_at, excerpt)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (item_id, f"h{item_id}", source, title, f"https://news.example.com/{item_id}", publisher,
         db.to_iso(published or NOW - timedelta(days=1)), db.to_iso(NOW), excerpt),
    )


def source_id(cfg):
    return next(source.id for source in cfg.sources if source.enabled)


def item(title, excerpt="", publisher=None, item_id=1, source="x"):
    return {"id": item_id, "title": title, "excerpt": excerpt, "publisher": publisher, "source_id": source,
            "published_at": db.to_iso(NOW)}


def terms(**overrides):
    base = dict(names=("GEP",), competitors=("Coupa", "SAP Ariba"), keywords=("sourcing",), excludes=("gene",),
                industry=("procurement", "supply chain"))
    base.update(overrides)
    return matching.Terms(**base)


# Matching ----------------------------------------------------------------------------------------


def test_client_name_in_headline_is_a_strong_match():
    match = matching.score_item(item("GEP launches procurement analytics"), terms(), min_score=1.5)
    assert match is not None and match.score >= 5


def test_short_name_alone_in_summary_does_not_match():
    # "GEP" in the summary with no industry context: not our client.
    assert matching.score_item(item("Chipmaker posts record quarter", excerpt="GEP appears in a footnote"), terms(), 1.5) is None


def test_short_name_in_summary_counts_with_industry_context():
    assert matching.score_item(item("Buyers rethink spend", excerpt="GEP and procurement teams agree"), terms(), 1.5)


def test_name_match_respects_word_boundaries():
    assert matching.score_item(item("GEPetto updates its procurement tools"), terms(), 1.5) is None


def test_competitor_is_a_strong_match():
    match = matching.score_item(item("Coupa expands its AI roadmap"), terms(), 1.5)
    assert match is not None and "Coupa" in match.reasons


def test_exclude_keyword_drops_the_story():
    assert matching.score_item(item("GEP gene therapy partnership"), terms(), 1.5) is None


def test_generic_industry_news_needs_two_industry_hits():
    assert matching.score_item(item("Supply chain disruption hits ports"), terms(), 1.5) is None
    assert matching.score_item(item("Supply chain and procurement budgets rise"), terms(), 1.5) is not None


def test_rank_caps_per_source_and_skips_featured_items():
    rows = [item(f"GEP news {n}", item_id=n, source="same") for n in range(1, 6)]
    picked = matching.rank(rows, terms(), min_score=1.5, max_stories=10, max_per_source=3)
    assert len(picked) == 3
    skipped = matching.rank(rows, terms(), min_score=1.5, max_stories=10, max_per_source=3, exclude_ids={1, 2})
    assert {match.item_id for match in skipped} == {3, 4, 5}


# Form ---------------------------------------------------------------------------------------------


def fields(**values):
    return {key: value if isinstance(value, list) else [value] for key, value in values.items()}


def test_name_and_industry_are_required(settings):
    _, errors, _ = profiles.parse_form(fields(client_name="G"), settings, is_new=True)
    assert any("name" in error for error in errors)
    assert any("industry" in error.lower() for error in errors)


def test_blank_optional_fields_are_prefilled_and_marked_as_suggestions(settings):
    data, errors, _ = profiles.parse_form(
        fields(client_name="GEP", industries=["supply_chain_procurement"]), settings, is_new=True
    )
    assert not errors
    assert "competitors" in data["suggested"] and "keywords" in data["suggested"]
    assert any(c["name"] == "Coupa" for c in data["competitors"])


def test_filled_fields_are_not_prefilled(settings):
    data, _, _ = profiles.parse_form(
        fields(client_name="GEP", industries=["supply_chain_procurement"], competitors="Acme | acme.com", keywords="x y"),
        settings, is_new=True,
    )
    assert data["suggested"] == ["keywords"] or data["suggested"] == []
    assert [c["name"] for c in data["competitors"]] == ["Acme"]


def test_website_must_be_a_real_address(settings):
    _, errors, _ = profiles.parse_form(
        fields(client_name="GEP", industries=["bfsi"], website="javascript:alert(1)"), settings, is_new=True
    )
    assert any("website" in error for error in errors)


def test_addresses_are_normalised_and_deduplicated():
    accepted, rejected = profiles.parse_addresses("Ann <Ann@RankUno.com>; ann@rankuno.com\nnot-an-address")
    assert accepted == ["ann@rankuno.com"]
    assert rejected and rejected[0][0] == "not-an-address"


# Store: invitations, sessions, unsubscribe -----------------------------------------------------


def test_invitation_works_once_and_expires(pconn):
    person = store.add_person(pconn, "m@rankuno.com", "M", "manager", NOW)
    raw = store.create_invite(pconn, person, "admin@rankuno.com", NOW, days=7)
    assert store.redeem_invite(pconn, raw, NOW + timedelta(hours=1)) is not None
    assert store.redeem_invite(pconn, raw, NOW + timedelta(hours=2)) is None  # used
    raw2 = store.create_invite(pconn, person, "admin@rankuno.com", NOW, days=7)
    assert store.redeem_invite(pconn, raw2, NOW + timedelta(days=8)) is None  # expired


def test_only_hashes_of_tokens_are_stored(pconn):
    person = store.add_person(pconn, "m@rankuno.com", "M", "manager", NOW)
    raw = store.create_invite(pconn, person, "a", NOW, days=7)
    stored = [row["token_hash"] for row in pconn.execute("SELECT token_hash FROM personal_invites")]
    assert raw not in stored and store.hash_token(raw) in stored


def test_deactivated_person_cannot_use_a_session(pconn):
    person = store.add_person(pconn, "m@rankuno.com", "M", "manager", NOW)
    raw, _ = store.create_session(pconn, person, NOW, 14)
    assert store.session_for(pconn, raw, NOW) is not None
    store.deactivate_person(pconn, person, NOW)
    assert store.session_for(pconn, raw, NOW) is None


def test_session_expires(pconn):
    person = store.add_person(pconn, "m@rankuno.com", "M", "manager", NOW)
    raw, _ = store.create_session(pconn, person, NOW, 14)
    assert store.session_for(pconn, raw, NOW + timedelta(days=15)) is None


def test_unsubscribe_stops_one_client_only(pconn):
    person = store.add_person(pconn, "m@rankuno.com", "M", "manager", NOW)
    one = store.create_client(pconn, person, data_for("GEP"), NOW)
    two = store.create_client(pconn, person, data_for("Other"), NOW)
    store.add_recipients(pconn, one, ["ann@rankuno.com"], NOW)
    store.add_recipients(pconn, two, ["ann@rankuno.com"], NOW)
    token = store.unsubscribe_token(pconn, one, "ann@rankuno.com")
    assert store.unsubscribe(pconn, token, NOW)["client_name"] == "GEP"
    assert store.active_recipient_rows(pconn, one) == []
    assert [row["address"] for row in store.active_recipient_rows(pconn, two)] == ["ann@rankuno.com"]
    assert store.unsubscribe(pconn, "not-a-token", NOW) is None


def data_for(name, suggested=()):
    return {"client_name": name, "website": "", "aliases": [], "industries": ["bfsi"], "competitors": [],
            "keywords": [], "excludes": [], "suggested": list(suggested)}


def test_unreviewed_suggestions_keep_a_client_out_of_the_run(pconn):
    person = store.add_person(pconn, "m@rankuno.com", "M", "manager", NOW)
    client = store.create_client(pconn, person, data_for("GEP", suggested=["keywords"]), NOW)
    store.add_recipients(pconn, client, ["ann@rankuno.com"], NOW)
    store.set_client_status(pconn, client, "active", NOW)
    assert store.active_clients(pconn) == []
    store.mark_reviewed(pconn, client, NOW)
    assert [row["id"] for row in store.active_clients(pconn)] == [client]


# Weekly run ------------------------------------------------------------------------------------


@dataclass
class FakeTransport:
    sent: list = field(default_factory=list)
    refuse: set = field(default_factory=set)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, message):
        from rankuno_brief.mailer import RecipientRejected

        if message["To"] in self.refuse:
            raise RecipientRejected("mailbox unknown", permanent=True)
        self.sent.append(message)


@dataclass
class FakeProfile:
    recipients: tuple = ("test@rankuno.com",)
    sender: str = "newsletter@rankuno.com"
    sender_name: str = "The RankUno Brief"
    reply_to: str | None = None
    transport_obj: FakeTransport = field(default_factory=FakeTransport)
    is_test: bool = False

    def subject(self, value):
        return value

    def transport(self):
        return self.transport_obj


@pytest.fixture
def weekly(monkeypatch, pconn, cfg):
    monkeypatch.setattr(feed.time, "sleep", lambda seconds: None)  # the real pause between sends is not needed here
    monkeypatch.setattr(feed, "personal_sending_enabled", lambda: True)
    monkeypatch.setattr(feed, "public_base_url", lambda: "https://brief.example.com")
    profile = FakeProfile()
    monkeypatch.setattr(feed, "load_profile", lambda name, cfg: profile)
    src = source_id(cfg)
    add_item(pconn, 1, "GEP launches new procurement suite", excerpt="Sourcing teams get AI", source=src, publisher="Reuters")
    add_item(pconn, 2, "Coupa raises prices for suppliers", excerpt="", source=src)
    add_item(pconn, 3, "Unrelated chip news", source=src)
    manager = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", NOW)
    client = store.create_client(pconn, manager, data_for("GEP") | {"industries": ["supply_chain_procurement"],
                                 "competitors": [{"name": "Coupa", "website": ""}], "keywords": ["sourcing"]}, NOW)
    store.add_recipients(pconn, client, ["ann@rankuno.com", "bob@rankuno.com"], NOW)
    store.set_client_status(pconn, client, "active", NOW - timedelta(days=7))  # news since activation counts
    return profile, client


def dns():
    return FakeDns(records={**MX})


def test_weekly_run_sends_one_email_per_person_with_unsubscribe(pconn, cfg, weekly):
    profile, client = weekly
    report = feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    assert sorted(report.sent) == ["ann@rankuno.com", "bob@rankuno.com"]
    assert len(profile.transport_obj.sent) == 2
    body = profile.transport_obj.sent[0].get_body(("html",)).get_content()
    assert "GEP launches new procurement suite" in body
    assert "Chip" not in body
    assert "https://brief.example.com/u/" in body
    assert "Reuters" in body


def test_a_run_cannot_repeat_for_the_same_date(pconn, cfg, weekly):
    profile, _ = weekly
    feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    again = feed.run_weekly(cfg, pconn, now=NOW + timedelta(hours=1), resolver=dns())
    assert again.sent == [] and len(profile.transport_obj.sent) == 2


def test_stories_are_not_repeated_the_following_week(pconn, cfg, weekly):
    profile, _ = weekly
    feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    next_week = feed.run_weekly(cfg, pconn, now=NOW + timedelta(days=7), resolver=dns())
    assert next_week.sent == []  # no new matching news: the client is skipped, not sent a repeat
    assert len(profile.transport_obj.sent) == 2


def test_client_with_no_news_is_skipped(pconn, cfg, weekly):
    profile, client = weekly
    pconn.execute("DELETE FROM items")
    report = feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    assert report.sent == [] and profile.transport_obj.sent == []


def test_refused_recipient_is_reported_and_others_still_get_mail(pconn, cfg, weekly):
    profile, _ = weekly
    profile.transport_obj.refuse = {"ann@rankuno.com"}
    report = feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    assert report.sent == ["bob@rankuno.com"]
    assert report.failed and report.failed[0][0] == "ann@rankuno.com"


def test_recipient_outside_the_allowlist_is_never_sent_to(pconn, cfg, weekly):
    profile, client = weekly
    store.add_recipients(pconn, client, ["someone@gmail.com"], NOW)
    report = feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    assert "someone@gmail.com" not in report.sent
    assert all(message["To"] != "someone@gmail.com" for message in profile.transport_obj.sent)


def test_weekly_run_does_nothing_when_sending_is_off(monkeypatch, pconn, cfg, weekly):
    profile, _ = weekly
    monkeypatch.setattr(feed, "personal_sending_enabled", lambda: False)
    feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    assert profile.transport_obj.sent == []
    assert store.run_for_date(pconn, NOW.astimezone(IST).date().isoformat()) is None


def test_test_run_goes_only_to_test_recipients_and_records_nothing(monkeypatch, pconn, cfg, weekly):
    profile, _ = weekly
    report = feed.run_weekly(cfg, pconn, now=NOW, test=True, resolver=dns())
    assert report.sent == ["test@rankuno.com"]
    assert all(message["To"] == "test@rankuno.com" for message in profile.transport_obj.sent)
    assert pconn.execute("SELECT COUNT(*) FROM personal_deliveries").fetchone()[0] == 0


def test_is_due_monday_ten_once(pconn, cfg, settings):
    monday = datetime(2026, 11, 2, 10, 5, tzinfo=IST)  # a Monday
    assert feed.is_due(monday, settings, pconn, IST)
    assert not feed.is_due(datetime(2026, 11, 2, 9, 59, tzinfo=IST), settings, pconn, IST)
    assert not feed.is_due(datetime(2026, 11, 3, 10, 5, tzinfo=IST), settings, pconn, IST)  # Tuesday
    store.start_run(pconn, monday.date().isoformat(), monday.astimezone(timezone.utc))
    assert not feed.is_due(monday, settings, pconn, IST)


# Email rendering -------------------------------------------------------------------------------


def test_email_escapes_news_text(cfg):
    section = mail.ClientSection(
        client_name="<b>GEP</b>", industries=("Supply",),
        stories=(mail.StoryLine("<script>x</script>", "https://a.example/?a=1&b=2", "Src", NOW, "<i>hi</i>", ()),),
        unsubscribe_url="https://brief.example.com/u/abc",
    )
    html_body, text_body = mail.weekly_email(cfg, NOW, [section])
    assert "<script>x</script>" not in html_body and "&lt;script&gt;" in html_body
    assert "<b>GEP</b>" not in html_body
    assert "https://brief.example.com/u/abc" in html_body
    assert "Stop receiving" in html_body


@pytest.fixture
def web_db(tmp_path, cfg):
    """The web pages open their own connection to cfg.db_path, so these tests use a real file in a temp folder."""
    from dataclasses import replace

    isolated = replace(cfg, data_root=tmp_path)
    connection = store.connect(isolated.db_path)
    yield isolated, connection
    connection.close()


# Web flow ---------------------------------------------------------------------------------------


class FakeHandler:
    def __init__(self, method, path, body="", cookie=None, origin=True):
        self.command = method
        self.path = path
        data = body.encode()
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(data)), "Host": "brief.example.com"}
        if origin:
            self.headers["Origin"] = "https://brief.example.com"
        if cookie:
            self.headers["Cookie"] = f"rb_session={cookie}"
        self.status = None
        self.out_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.out_headers.append((name, value))

    def end_headers(self):
        pass

    def _security_headers(self):
        pass

    def header(self, name):
        return next((v for n, v in self.out_headers if n == name), None)

    @property
    def text(self):
        return self.wfile.getvalue().decode("utf-8")


def call(web_app, method, path, body="", cookie=None, origin=True):
    handler = FakeHandler(method, path, body, cookie, origin)
    if method == "GET":
        handler.headers.pop("Content-Length", None)
    web_app.handle(handler, method)
    return handler


def test_sign_in_flow_and_client_form(monkeypatch, web_db):
    cfg, pconn = web_db
    import urllib.parse as up

    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    person = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    raw = store.create_invite(pconn, person, "admin", datetime.now(timezone.utc), days=7)

    shown = call(app, "GET", f"/m/{raw}")
    assert shown.status == 200 and "Sign in" in shown.text

    signed = call(app, "POST", f"/m/{raw}")
    assert signed.status == 303 and signed.header("Location") == "/p/"
    cookie = signed.header("Set-Cookie").split(";")[0].split("=", 1)[1]

    # Link is one use: a second POST is refused.
    assert call(app, "POST", f"/m/{raw}").status == 400

    page = call(app, "GET", "/p/clients/new", cookie=cookie)
    assert page.status == 200 and 'name="csrf"' in page.text
    csrf = page.text.split('name="csrf" value="')[1].split('"')[0]

    # Without the CSRF token nothing is saved.
    refused = call(app, "POST", "/p/clients", "client_name=GEP&industries=bfsi&action=save", cookie=cookie)
    assert refused.status == 403

    body = up.urlencode([
        ("csrf", csrf), ("client_name", "GEP"), ("industries", "supply_chain_procurement"),
        ("recipients", "ann@rankuno.com"), ("action", "save"),
    ])
    saved = call(app, "POST", "/p/clients", body, cookie=cookie)
    assert saved.status == 303
    client_id = int(saved.header("Location").split("/")[3].split("?")[0])
    row = store.client(pconn, client_id)
    assert row["manager_id"] == person and row["status"] == "draft"
    assert "competitors" in store.client_suggested(row)

    # Not active yet: suggestions unreviewed.
    activate = up.urlencode([("csrf", csrf), ("client_name", "GEP"), ("industries", "supply_chain_procurement"),
                             ("recipients", ""), ("activate", "1"), ("action", "save")])
    blocked = call(app, "POST", f"/p/clients/{client_id}", activate, cookie=cookie)
    assert blocked.status == 200 and "Confirm the suggested values" in blocked.text
    assert store.client(pconn, client_id)["status"] == "draft"

    confirm = up.urlencode([("csrf", csrf), ("client_name", "GEP"), ("industries", "supply_chain_procurement"),
                            ("recipients", ""), ("activate", "1"), ("reviewed", "1"), ("action", "save")])
    done = call(app, "POST", f"/p/clients/{client_id}", confirm, cookie=cookie)
    assert done.status == 303
    assert store.client(pconn, client_id)["status"] == "active"


def test_managers_cannot_open_each_others_clients(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    owner = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    other = store.add_person(pconn, "other@rankuno.com", "Other", "manager", REAL)
    client = store.create_client(pconn, owner, data_for("GEP"), REAL)
    raw, _ = store.create_session(pconn, other, REAL, 14)
    assert call(app, "GET", f"/p/clients/{client}", cookie=raw).status == 404
    assert call(app, "GET", f"/p/clients/{client}/preview", cookie=raw).status == 404


def test_admin_invite_needs_an_allowed_address(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    admin = store.add_person(pconn, "rajat.singh@rankuno.com", "Rajat", "admin", REAL)
    raw, csrf = store.create_session(pconn, admin, REAL, 14)
    import urllib.parse as up

    body = up.urlencode([("csrf", csrf), ("email", "someone@gmail.com"), ("role", "manager")])
    result = call(app, "POST", "/a/people", body, cookie=raw)
    assert result.status == 200 and "not in recipients.allowed_domains" in result.text
    assert store.person_by_email(pconn, "someone@gmail.com") is None

    body = up.urlencode([("csrf", csrf), ("email", "naeem.gari@rankuno.com"), ("name", "Naeem"), ("role", "manager")])
    result = call(app, "POST", "/a/people", body, cookie=raw)
    assert result.status == 200 and "/m/" in result.text  # shown once, email sending is off
    assert store.person_by_email(pconn, "naeem.gari@rankuno.com")["role"] == "manager"


def test_unsubscribe_page_and_post(web_db):
    cfg, pconn = web_db
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    person = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    client = store.create_client(pconn, person, data_for("GEP"), REAL)
    store.add_recipients(pconn, client, ["ann@rankuno.com"], REAL)
    token = store.unsubscribe_token(pconn, client, "ann@rankuno.com")
    assert "Stop sending" in call(app, "GET", f"/u/{token}").text
    assert "will no longer receive GEP" in call(app, "POST", f"/u/{token}").text
    assert store.active_recipient_rows(pconn, client) == []
    assert call(app, "GET", "/u/unknown").status == 404


def test_pages_are_served_by_the_real_http_server(monkeypatch, web_db):
    """End to end over a socket: the admin server routes /m/, /u/ and /p/ to the personal pages."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from rankuno_brief.server import AdminHandler, App

    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    monkeypatch.setattr("rankuno_brief.server.public_base_url", lambda: "http://localhost")
    app = App(cfg, token="k" * 40)
    assert app.personal is not None
    person = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    raw = store.create_invite(pconn, person, "admin", REAL, days=7)
    unsub_client = store.create_client(pconn, person, data_for("GEP"), REAL)
    store.add_recipients(pconn, unsub_client, ["ann@rankuno.com"], REAL)
    unsub = store.unsubscribe_token(pconn, unsub_client, "ann@rankuno.com")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), AdminHandler)
    httpd.app = app
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/m/{raw}") as response:
            assert response.status == 200 and b"Sign in" in response.read()
        with urllib.request.urlopen(f"{base}/u/{unsub}") as response:
            assert b"Stop sending" in response.read()
        # The admin page still asks for ADMIN_TOKEN: personal routes did not open it.
        try:
            urllib.request.urlopen(f"{base}/")
            raise AssertionError("admin page should need its token")
        except urllib.error.HTTPError as exc:
            assert exc.code in (401, 404)
    finally:
        httpd.shutdown()
        httpd.server_close()
