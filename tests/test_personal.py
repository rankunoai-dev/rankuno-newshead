from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from rankuno_brief import db
from rankuno_brief.personal import feed, mail, matching, notify, profiles, store, web
import urllib.parse as up
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
            "published_at": db.to_iso(NOW), "url": f"https://news.example.com/{item_id}"}


def terms(**overrides):
    base = dict(names=("GEP",), competitors=("Coupa", "SAP Ariba"), core=("strategic sourcing",), operational=(),
                broad=("procurement", "supply chain"), excludes=("gene",))
    base.update(overrides)
    return matching.Terms(**base)


# Matching ----------------------------------------------------------------------------------------


def test_client_name_in_headline_is_a_strong_match():
    match = matching.score_item(item("GEP launches procurement analytics"), terms(), min_score=1.5)
    assert match is not None and match.score >= 5


def test_short_name_alone_in_summary_does_not_match():
    # "GEP" in the summary with no industry context: not our client.
    assert matching.score_item(item("Chipmaker posts record quarter", excerpt="GEP appears in a footnote"), terms(), 1.5) is None


def test_short_name_in_summary_counts_with_broad_context():
    assert matching.score_item(item("Buyers rethink spend", excerpt="GEP and procurement and supply chain teams agree"), terms(), 1.5)


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
            "keywords": [], "excludes": [], "suggested": list(suggested), "send_day": 6}  # 6 = Sunday


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
    monkeypatch.setattr(feed.analysis.articles, "ensure", lambda *a, **k: {})  # no article reads in tests
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
    assert "REUTERS" in body  # the source is set in capitals, as in the general brief


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
    assert feed.is_due(datetime(2026, 11, 3, 10, 5, tzinfo=IST), settings, pconn, IST)  # the daily run: Tuesday too
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
    store.approve_person(pconn, person, REAL)  # an approved manager can activate clients
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


# Approval, admin decisions and notices ------------------------------------------------------------


class Sent:
    def __init__(self):
        self.items = []

    def __call__(self, cfg, to, subject, html_body, text_body):
        self.items.append((to, subject))
        return True


@pytest.fixture
def mailbox(monkeypatch):
    sent = Sent()
    monkeypatch.setattr(notify, "personal_sending_enabled", lambda: True)
    monkeypatch.setattr(notify, "send", sent)
    return sent


def form_body(csrf, **values):
    base = {"client_name": "GEP", "industries": "supply_chain_procurement", "recipients": ""}
    base.update(values)
    pairs = [("csrf", csrf)]
    for key, value in base.items():
        if key == "industries":
            pairs += [("industries", value)]
        else:
            pairs.append((key, value))
    return up.urlencode(pairs)


def test_unapproved_manager_can_draft_but_not_activate(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    manager = store.add_person(pconn, "new@rankuno.com", "New", "manager", REAL)
    raw, csrf = store.create_session(pconn, manager, REAL, 14)
    result = call(app, "POST", "/p/clients", form_body(csrf, activate="1", action="save"), cookie=raw)
    client = store.clients_of(pconn, manager)[0]
    assert client["status"] == "draft"
    assert "waiting for admin approval" in result.text


def test_request_is_sent_to_all_default_admins_and_first_decision_wins(monkeypatch, web_db, mailbox):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    manager = store.add_person(pconn, "new@rankuno.com", "New", "manager", REAL)
    store.create_client(pconn, manager, data_for("GEP"), REAL)
    raw, csrf = store.create_session(pconn, manager, REAL, 14)
    call(app, "POST", "/p/request", up.urlencode([("csrf", csrf)]), cookie=raw)
    assert sorted(to for to, _ in mailbox.items) == sorted(
        ["rajat.singh@rankuno.com", "naeem.gari@rankuno.com", "prithviraj.pednekar@rankuno.com"])
    # A second submit while waiting does not open another request.
    call(app, "POST", "/p/request", up.urlencode([("csrf", csrf)]), cookie=raw)
    assert len(mailbox.items) == 3
    request_id = store.pending_request_for(pconn, manager)["id"]

    admin = store.person_by_email(pconn, "naeem.gari@rankuno.com")
    admin_raw, admin_csrf = store.create_session(pconn, admin["id"], REAL, 14)
    approved = call(app, "POST", f"/a/requests/{request_id}/approve", up.urlencode([("csrf", admin_csrf)]), cookie=admin_raw)
    assert approved.status == 200 and "Request approved" in approved.text
    assert store.is_approved(store.person(pconn, manager))
    # The other admins hear about the decision, and the manager is told.
    assert any(to == "new@rankuno.com" for to, _ in mailbox.items)

    rajat = store.person_by_email(pconn, "rajat.singh@rankuno.com")
    rajat_raw, rajat_csrf = store.create_session(pconn, rajat["id"], REAL, 14)
    again = call(app, "POST", f"/a/requests/{request_id}/approve", up.urlencode([("csrf", rajat_csrf)]), cookie=rajat_raw)
    assert "already decided" in again.text


def test_rejection_needs_a_reason_and_reaches_the_manager(monkeypatch, web_db, mailbox):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    manager = store.add_person(pconn, "new@rankuno.com", "New", "manager", REAL)
    store.create_client(pconn, manager, data_for("GEP"), REAL)
    store.submit_request(pconn, manager, REAL)
    request_id = store.pending_request_for(pconn, manager)["id"]
    admin = store.person_by_email(pconn, "naeem.gari@rankuno.com")
    raw, csrf = store.create_session(pconn, admin["id"], REAL, 14)
    no_reason = call(app, "POST", f"/a/requests/{request_id}/reject", up.urlencode([("csrf", csrf), ("reason", " ")]), cookie=raw)
    assert "Give a reason" in no_reason.text
    assert store.pending_request_for(pconn, manager) is not None
    call(app, "POST", f"/a/requests/{request_id}/reject", up.urlencode([("csrf", csrf), ("reason", "Client name is unclear")]), cookie=raw)
    assert store.request_row(pconn, request_id)["status"] == "rejected"
    assert not store.is_approved(store.person(pconn, manager))
    assert any(to == "new@rankuno.com" for to, _ in mailbox.items)


def test_admin_edits_a_managers_client_and_the_change_is_audited(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    manager = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    store.approve_person(pconn, manager, REAL)
    client = store.create_client(pconn, manager, data_for("GEP"), REAL)
    admin = store.person_by_email(pconn, "rajat.singh@rankuno.com")
    raw, csrf = store.create_session(pconn, admin["id"], REAL, 14)
    body = form_body(csrf, client_name="GEP Worldwide", action="save")
    result = call(app, "POST", f"/p/clients/{client}", body, cookie=raw)
    assert result.status == 303
    assert store.client(pconn, client)["client_name"] == "GEP Worldwide"
    actions = [(row["actor"], row["action"]) for row in store.recent_audit(pconn)]
    assert ("rajat.singh@rankuno.com", "client.update") in actions


def test_manager_edit_of_an_active_client_notifies_the_admins(monkeypatch, web_db, mailbox):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    manager = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    store.approve_person(pconn, manager, REAL)
    client = store.create_client(pconn, manager, data_for("GEP"), REAL)
    store.add_recipients(pconn, client, ["ann@rankuno.com"], REAL)
    store.set_client_status(pconn, client, "active", REAL)
    raw, csrf = store.create_session(pconn, manager, REAL, 14)
    call(app, "POST", f"/p/clients/{client}", form_body(csrf, client_name="GEP Inc", action="save"), cookie=raw)
    assert sorted(to for to, subject in mailbox.items if subject.startswith("Client changed")) == sorted(
        ["rajat.singh@rankuno.com", "naeem.gari@rankuno.com", "prithviraj.pednekar@rankuno.com"])


PASSWORD = "correct horse battery"


def set_shared(conn, password=PASSWORD):
    store.set_setting(conn, store.SHARED_USERNAME_KEY, "admin")
    store.set_shared_password(conn, password)


def test_shared_login_opens_the_dashboard_and_wrong_password_does_not(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)
    set_shared(pconn)
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    bad = call(app, "POST", "/login", up.urlencode([("username", "admin"), ("password", "wrong")]))
    assert bad.status == 200 and "not correct" in bad.text
    good = call(app, "POST", "/login", up.urlencode([("username", "admin"), ("password", PASSWORD)]))
    assert good.status == 303 and good.header("Location") == "/a/"
    cookie = good.header("Set-Cookie").split(";")[0].split("=", 1)[1]
    dashboard = call(app, "GET", "/a/", cookie=cookie)
    assert dashboard.status == 200 and "Shared admin login" in dashboard.text


def test_dashboard_redirects_to_login_without_a_session(web_db):
    cfg, _ = web_db
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    assert call(app, "GET", "/a/").status == 303


def test_admin_changes_the_shared_password_and_the_old_one_stops_working(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)
    set_shared(pconn)
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    good = call(app, "POST", "/login", up.urlencode([("username", "admin"), ("password", PASSWORD)]))
    cookie = good.header("Set-Cookie").split(";")[0].split("=", 1)[1]
    csrf = store.session_for(pconn, cookie, REAL)["csrf"]
    body = up.urlencode([("csrf", csrf), ("current", "wrong-current-pw"), ("new", "a brand new password"), ("confirm", "a brand new password")])
    assert "not correct" in call(app, "POST", "/a/password", body, cookie=cookie).text
    body = up.urlencode([("csrf", csrf), ("current", PASSWORD), ("new", "a brand new password"), ("confirm", "a brand new password")])
    assert "was changed" in call(app, "POST", "/a/password", body, cookie=cookie).text
    assert not store.check_shared_login(pconn, "admin", PASSWORD)
    assert store.check_shared_login(pconn, "admin", "a brand new password")
    actions = [row["action"] for row in store.recent_audit(pconn)]
    assert "password.change" in actions


def test_short_passwords_are_refused(web_db):
    _, pconn = web_db
    with pytest.raises(ValueError):
        store.set_shared_password(pconn, "short")


def test_the_shared_login_is_never_emailed(web_db):
    _, pconn = web_db
    store.ensure_shared_admin(pconn, REAL)
    emails = [person["email"] for person in notify.admins(pconn)]
    assert store.SHARED_EMAIL not in emails
    assert "rajat.singh@rankuno.com" in emails


def test_status_page_accepts_the_shared_username_and_password(monkeypatch, web_db):
    import base64

    from rankuno_brief.server import App

    cfg, pconn = web_db
    set_shared(pconn)
    app = App(cfg, token="k" * 40)
    good = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()
    wrong = "Basic " + base64.b64encode(b"admin:nope-not-it").decode()
    assert app.shared_login_ok(good)
    assert not app.shared_login_ok(wrong)


def test_first_start_takes_the_password_from_admin_password_once(monkeypatch, web_db):
    from rankuno_brief.server import App

    cfg, pconn = web_db
    monkeypatch.setenv("ADMIN_PASSWORD", "first start password")
    App(cfg, token="k" * 40)
    assert store.check_shared_login(pconn, "admin", "first start password")
    monkeypatch.setenv("ADMIN_PASSWORD", "a different one entirely")
    App(cfg, token="k" * 40)  # already set: the environment variable is ignored now
    assert store.check_shared_login(pconn, "admin", "first start password")


# Custom operating industry ------------------------------------------------------------------------


def test_a_typed_industry_is_accepted_on_its_own(settings):
    data, errors, _ = profiles.parse_form(
        fields(client_name="Acme", custom_industry="Logistics & 3PL"), settings, is_new=True
    )
    assert not errors
    assert data["industries"] == ["custom:Logistics & 3PL"]
    assert data["competitors"] == [] and data["keywords"] == []  # nothing invented for a custom industry


def test_a_custom_industry_is_a_strong_keyword_of_its_own_name(settings):
    assert profiles.industry_terms(["custom:Logistics & 3PL"], settings) == ()
    assert profiles.custom_keywords(["custom:Logistics & 3PL", "bfsi"]) == ("Logistics & 3PL",)
    terms = matching.Terms(names=("Acme",), competitors=(), core=("Logistics & 3PL",), operational=(), broad=(), excludes=())
    assert matching.score_item(item("Logistics & 3PL firms expand into Europe"), terms, min_score=1.5) is not None


def test_industry_is_still_required_with_custom_box_empty(settings):
    _, errors, _ = profiles.parse_form(fields(client_name="Acme"), settings, is_new=True)
    assert any("industry" in error.lower() for error in errors)


def test_custom_industry_must_be_a_sensible_length(settings):
    _, errors, _ = profiles.parse_form(fields(client_name="Acme", custom_industry="x"), settings, is_new=True)
    assert any("custom industry" in error for error in errors)


def test_custom_industry_title_and_known_industries_still_resolve(settings):
    assert profiles.industry_title("custom:Logistics", settings) == "Logistics"
    assert profiles.industry_title("bfsi", settings) == settings.industry("bfsi").title


def test_form_shows_the_custom_box_with_the_saved_value(web_db):
    cfg, pconn = web_db
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    person = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    raw, _ = store.create_session(pconn, person, REAL, 14)
    client = store.create_client(pconn, person, data_for("Acme") | {"industries": ["custom:Logistics & 3PL"]}, REAL)
    page = call(app, "GET", f"/p/clients/{client}", cookie=raw)
    assert page.status == 200
    assert 'name="custom_industry" value="Logistics &amp; 3PL"' in page.text


# Feed day chosen per client -------------------------------------------------------------------


WEDNESDAY_0500_UTC = datetime(2026, 9, 16, 5, 0, tzinfo=timezone.utc)  # 10:30 IST on a Wednesday


def set_feed_day(conn, client_id, day):
    conn.execute("UPDATE personal_clients SET send_day = ? WHERE id = ?", (day, client_id))


def test_a_client_is_sent_only_on_its_feed_day(monkeypatch, pconn, cfg, weekly):
    profile, client = weekly
    set_feed_day(pconn, client, 2)  # Wednesday
    sunday = feed.run_weekly(cfg, pconn, now=NOW, resolver=dns())
    assert sunday.sent == [] and profile.transport_obj.sent == []
    wednesday = feed.run_weekly(cfg, pconn, now=WEDNESDAY_0500_UTC, resolver=dns())
    assert sorted(wednesday.sent) == ["ann@rankuno.com", "bob@rankuno.com"]


def test_a_client_gets_at_most_one_feed_a_week_even_if_its_day_changes(monkeypatch, pconn, cfg, weekly):
    profile, client = weekly
    set_feed_day(pconn, client, 2)
    feed.run_weekly(cfg, pconn, now=WEDNESDAY_0500_UTC, resolver=dns())
    set_feed_day(pconn, client, 3)  # the manager moves it to Thursday
    thursday = feed.run_weekly(cfg, pconn, now=WEDNESDAY_0500_UTC + timedelta(days=1), resolver=dns())
    assert thursday.sent == []  # only one day after the last feed
    assert len(profile.transport_obj.sent) == 2


def test_the_form_saves_the_feed_day_and_defaults_to_monday(settings):
    data, errors, _ = profiles.parse_form(fields(client_name="Acme", industries=["bfsi"], send_day="4"), settings, is_new=True)
    assert not errors and data["send_day"] == 4
    default, errors, _ = profiles.parse_form(fields(client_name="Acme", industries=["bfsi"]), settings, is_new=True)
    assert default["send_day"] == 0
    _, errors, _ = profiles.parse_form(fields(client_name="Acme", industries=["bfsi"], send_day="9"), settings, is_new=True)
    assert any("day of the week" in error for error in errors)


def test_an_admin_can_add_another_admin_from_the_dashboard(monkeypatch, web_db, mailbox):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    admin = store.person_by_email(pconn, "rajat.singh@rankuno.com")
    raw, csrf = store.create_session(pconn, admin["id"], REAL, 14)
    dashboard = call(app, "GET", "/a/", cookie=raw)
    assert '<option value="admin">Admin</option>' in dashboard.text
    body = up.urlencode([("csrf", csrf), ("email", "shreya.kale@rankuno.com"), ("name", "Shreya Kale"), ("role", "admin")])
    result = call(app, "POST", "/a/people", body, cookie=raw)
    assert result.status == 200 and "sign-in link" in result.text.lower()
    new_admin = store.person_by_email(pconn, "shreya.kale@rankuno.com")
    assert new_admin["role"] == "admin" and store.is_approved(new_admin)
    assert "shreya.kale@rankuno.com" in [person["email"] for person in notify.admins(pconn)]
    assert store.recent_audit(pconn)[0]["action"] == "invite.create"


def test_reassign_menu_shows_the_current_owner_and_admins(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web, "DnsResolver", lambda *a, **k: dns())
    app = web.PersonalWeb(cfg, "https://brief.example.com")
    shared = store.ensure_shared_admin(pconn, REAL)
    client = store.create_client(pconn, shared["id"], data_for("GEP"), REAL)
    admin = store.person_by_email(pconn, "rajat.singh@rankuno.com")
    raw, _ = store.create_session(pconn, admin["id"], REAL, 14)
    page = call(app, "GET", "/a/", cookie=raw)
    assert f'<option value="{shared["id"]}" selected>shared-admin</option>' in page.text
    assert "naeem.gari@rankuno.com" in page.text
    assert client  # the client exists in the menu


def test_delete_needs_the_shared_password_and_removes_the_client(monkeypatch, web_db):
    cfg, pconn = web_db
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)
    set_shared(pconn)
    manager = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", REAL)
    store.approve_person(pconn, manager, REAL)
    client = store.create_client(pconn, manager, data_for("GEP"), REAL)
    store.add_recipients(pconn, client, ["ann@rankuno.com"], REAL)
    admin = store.person_by_email(pconn, "rajat.singh@rankuno.com")
    raw, csrf = store.create_session(pconn, admin["id"], REAL, 14)
    app = web.PersonalWeb(cfg, "https://brief.example.com")

    wrong = up.urlencode([("csrf", csrf), ("password", "not the password at all")])
    refused = call(app, "POST", f"/a/clients/{client}/delete", wrong, cookie=raw)
    assert "Nothing was deleted" in refused.text
    assert store.client(pconn, client) is not None

    right = up.urlencode([("csrf", csrf), ("password", PASSWORD)])
    done = call(app, "POST", f"/a/clients/{client}/delete", right, cookie=raw)
    assert "was deleted" in done.text
    assert store.client(pconn, client) is None
    assert store.recipients_of(pconn, client) == []
    assert any(row["action"] == "client.delete" for row in store.recent_audit(pconn))


# Upgrades: tiers, executive moves, content type, layout -----------------------------------------


def test_broad_word_alone_in_a_headline_is_not_enough():
    # "spend management" is a broad word: one of them is not a reason to send a story.
    assert matching.score_item(item("Travel and spend management platform launches"), terms(broad=("spend management",)), 1.5) is None


def test_operational_topic_in_the_headline_is_kept_but_not_strategic():
    match = matching.score_item(item("Georgia-Pacific automates yard management"),
                                terms(core=(), operational=("yard management",)), 1.5)
    assert match is not None and match.strategic is False


def test_a_direct_competitor_or_client_makes_a_story_strategic():
    match = matching.score_item(item("Coupa expands its AI roadmap"), terms(), 1.5)
    assert match is not None and match.strategic is True


def test_executive_move_about_an_unrelated_company_is_dropped():
    assert matching.score_item(item("Wella appoints chief supply chain officer", excerpt="supply chain and logistics"),
                               terms(), 1.5) is None


def test_executive_move_about_a_client_or_competitor_is_kept():
    assert matching.score_item(item("GEP appoints a new chief financial officer"), terms(), 1.5) is not None
    assert matching.score_item(item("Coupa names a new CEO"), terms(), 1.5) is not None


def test_content_type_is_detected_from_url_and_wording():
    base = {"id": 1, "publisher": None, "excerpt": "", "title": "Context changes everything", "published_at": db.to_iso(NOW)}
    assert matching.content_type({**base, "url": "https://www.supplychaindive.com/spons/context-changes/"}) == "sponsored"
    assert matching.content_type({**base, "url": "https://x.example/a", "excerpt": "In this eBook, learn more"}) == "vendor"
    assert matching.content_type({**base, "url": "https://x.example/a", "excerpt": "Kroger changes its plans"}) == "editorial"


def test_client_keywords_are_placed_in_the_right_tier(settings):
    core, operational, broad = feed.tiered_keywords(("spend management", "direct sourcing", "yard management"), (), settings)
    assert "spend management" in broad and "spend management" not in core
    assert "direct sourcing" in core
    assert "yard management" in operational


def test_each_client_section_splits_strategic_from_industry_stories(cfg):
    strategic = mail.StoryLine("Kroger accelerates direct sourcing", "https://a.example/1", "Supply Chain Dive", NOW,
                               "Kroger is taking more buying in house.", ("direct sourcing",), "editorial", True)
    industry = mail.StoryLine("Georgia-Pacific automates yard management", "https://a.example/2", "Supply Chain Brain", NOW,
                              "", ("yard management",), "vendor", False)
    section = mail.ClientSection("GEP", ("Supply chain",), (strategic, industry), None)
    html_body, text_body = mail.weekly_email(cfg, NOW, [section])
    assert "STRATEGIC CLIENT &amp; MARKET MOVES" in html_body
    assert "INDUSTRY &amp; VENDOR INTELLIGENCE" in html_body
    assert "STRATEGIC CLIENT & MARKET MOVES" in text_body
    assert "VENDOR REPORT" in html_body and "Why it matters to GEP" in html_body
    assert "[VENDOR REPORT]" in text_body


# Article reading, takeaways, actions and mentions ------------------------------------------------


from rankuno_brief.personal import analysis, articles  # noqa: E402

ARTICLE_HTML = (
    '<html><head><title>x</title></head><body><nav><a href="https://other.example/home">Home</a></nav>'
    '<article><p>GEP announced a new supplier risk module for its procurement platform this quarter.</p>'
    '<p>Analysts said the release targets strategic sourcing teams with better spend visibility.</p>'
    '<p>Read <a href="https://www.gep.com/products">our product page</a> for details.</p></article></body></html>'
)


def gep_terms():
    return matching.Terms(names=("GEP",), competitors=("Coupa",), core=("supplier risk",),
                          operational=(), broad=("procurement",), excludes=())


def test_article_text_comes_from_the_article_block_and_links_are_kept():
    article = articles.extract(ARTICLE_HTML, "https://news.example.com/story")
    assert "GEP announced a new supplier risk module" in article.body
    assert "Home" not in article.body  # navigation is outside the article block
    assert "https://www.gep.com/products" in article.links


def test_takeaways_are_the_sentences_with_the_most_relevant_terms():
    body = ("The market moved this week. GEP announced a new supplier risk module for its procurement platform. "
            "Weather was mild across the region for most of the period.")
    takeaways = analysis.takeaways(body, gep_terms())
    assert takeaways and "GEP announced" in takeaways[0]
    assert all("Weather" not in line for line in takeaways)


def test_suggested_actions_follow_the_rules():
    actions = analysis.suggested_actions("Coupa expands its AI roadmap", "", gep_terms(), client_name="Acme",
                                         content_type="sponsored", strategic=True)
    assert any(action.startswith("Sponsored content") for action in actions)
    assert any("Competitor activity (Coupa)" in action and "Acme's positioning" in action for action in actions)
    assert all("GEP" not in action for action in actions)  # the wording follows the client, whoever it is
    assert len(actions) <= 2


def test_mentions_are_linked_when_the_article_links_to_the_client_site():
    item = {"id": 1, "title": "Industry note", "excerpt": "", "publisher": "Example News",
            "url": "https://news.example.com/story"}
    linked = articles.ArticleText(body="GEP is named here.", links=("https://www.gep.com/products",))
    unlinked = articles.ArticleText(body="GEP is named here.", links=("https://other.example/x",))
    assert analysis.find_mentions(item, linked, gep_terms(), "https://www.gep.com/")[0].kind == "linked"
    assert analysis.find_mentions(item, unlinked, gep_terms(), "https://www.gep.com/")[0].kind == "unlinked"
    assert analysis.find_mentions(item, unlinked, gep_terms(), "")[0].kind == "unknown"


def test_competitor_mentions_are_found_in_the_article_text_not_only_the_headline():
    item = {"id": 2, "title": "Buyers compare vendors", "excerpt": "", "publisher": "Trade Daily",
            "url": "https://trade.example/x"}
    article = articles.ArticleText(body="Many buyers already use Coupa for invoices.", links=())
    found = analysis.find_competitors(item, article, gep_terms())
    assert found and found[0].kind == "competitor" and found[0].term == "Coupa"


def test_articles_are_read_once_and_then_served_from_the_cache(monkeypatch, pconn):
    calls = []

    def fake_fetch(client, url):
        calls.append(url)
        return articles.ArticleText(body="Cached body text. " * 8, links=())

    monkeypatch.setattr(articles, "fetch", fake_fetch)
    monkeypatch.setattr(articles.time, "sleep", lambda seconds: None)
    pconn.execute("INSERT INTO items (id, url_hash, source_id, title, url, published_at, fetched_at, excerpt) "
                  "VALUES (50, 'h50', 'x', 'T', 'https://news.example.com/50', ?, ?, '')", (db.to_iso(NOW), db.to_iso(NOW)))
    items = [{"id": 50, "url": "https://news.example.com/50"}]
    first = articles.ensure(pconn, items, now=NOW, user_agent="test")
    second = articles.ensure(pconn, items, now=NOW + timedelta(days=1), user_agent="test")
    assert first[50].body == second[50].body and first[50].body.startswith("Cached body text.")
    assert calls == ["https://news.example.com/50"]


def test_the_email_shows_mentions_and_competitor_watch(cfg):
    mention = analysis.Mention("unlinked", "GEP", "Trade Daily", "Buyers compare vendors",
                               "https://trade.example/x", "GEP is named here.")
    rival = analysis.Mention("competitor", "Coupa", "Trade Daily", "Coupa news", "https://trade.example/y", "Coupa said.")
    story = mail.StoryLine("A story", "https://a.example/1", "Trade Daily", NOW, "Excerpt.", ("supplier risk",),
                           "editorial", True, takeaways=("GEP announced a module.",),
                           actions=("Core topic (supplier risk): consider a sales angle.",))
    section = mail.ClientSection("GEP", ("Supply chain",), (story,), None,
                                 mentions=(mention,), competitor_watch=(rival,))
    html_body, text_body = mail.weekly_email(cfg, NOW, [section])
    assert "WHERE GEP IS MENTIONED" in html_body and "Named, not linked" in html_body
    assert "COMPETITOR WATCH" in html_body
    assert "Key takeaways" in html_body and "Suggested action:" in html_body
    assert "Suggested action: Core topic" in text_body


# Per-client searches ----------------------------------------------------------------------------


def test_each_active_client_gets_one_search_from_its_names_and_competitors(pconn):
    from rankuno_brief.personal import client_news

    manager = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", NOW)
    acme = store.create_client(pconn, manager, data_for("Acme Corp") | {
        "aliases": ["Acme Cloud"], "competitors": [{"name": "Globex", "website": ""}]}, NOW)
    store.set_client_status(pconn, acme, "active", NOW)
    store.create_client(pconn, manager, data_for("Draft Only"), NOW)  # still a draft: no search yet
    sources = client_news.sources_for(pconn)
    assert [source.id for source in sources] == [f"client-news-{acme}"]
    assert sources[0].query == '("Acme Corp" OR "Acme Cloud" OR "Globex")'
    assert client_news.is_client_news(sources[0].id) and sources[0].personal_only


def test_client_searches_are_fetched_separately_and_stay_out_of_the_general_brief(monkeypatch, cfg, pconn):
    from rankuno_brief.personal import client_news

    manager = store.add_person(pconn, "lead@rankuno.com", "Lead", "manager", NOW)
    client = store.create_client(pconn, manager, data_for("Acme Corp"), NOW)
    store.set_client_status(pconn, client, "active", NOW)
    seen = {}

    class Summary:
        ok, failed, new_items = 1, 0, 0

    def fake_run(run_cfg, conn):
        seen["sources"] = [source.id for source in run_cfg.sources]
        return Summary()

    monkeypatch.setattr(client_news.fetch, "run_fetch", fake_run)
    client_news.fetch_client_news(cfg, pconn)
    assert seen["sources"] == [f"client-news-{client}"]
    assert all(source.id.startswith("client-news-") is False for source in cfg.sources if source.personal_only is False)
