"""Web pages of the personalised feed, mounted in the same server as the admin page.

  /m/<token>                              sign-in from an emailed link (GET shows a button; POST uses the link, once)
  /logout                                 end the session
  /p/                                     a manager's clients and approval status
  /p/request                              a manager asks admins to approve their account
  /p/clients/new, /p/clients/<id>         the client form (managers: own clients; admins: any)
  /p/clients/<id>/preview                 this week's feed for the client, on screen only (nothing is sent)
  /a/                                     admin: approval requests, people, clients, audit log
  /a/requests/<id>/approve|reject         admin decision on a manager's request (the first decision wins)
  /u/<token>                              one-click unsubscribe from one client's feed

Sign-in is only by emailed link. Managers are approved by an admin before they can activate clients.
Every POST from a signed-in page carries a CSRF token and must come from this site.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import db
from ..config import Config
from ..security.dns import DnsResolver
from ..security.recipients import check_recipients, normalize_address
from . import feed as feed_runner
from . import mail, notify, profiles, store
from .settings import PersonalSettings, cached_settings, personal_sending_enabled

log = logging.getLogger(__name__)

COOKIE = "rb_session"
MAX_BODY = 64_000
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
PREVIEW_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"

STYLE = """
body { font-family: Arial, Helvetica, sans-serif; color: #262626; background: #F2F2F2; margin: 0; font-size: 14px; line-height: 21px; }
.wrap { max-width: 920px; margin: 0 auto; padding: 0 24px; box-sizing: border-box; }
.masthead { background: #DF212A; color: #FFFFFF; padding: 16px 0; }
.masthead .wrap { display: flex; align-items: center; gap: 18px; }
.masthead img { display: block; }
.masthead .product { font-size: 13px; letter-spacing: 1px; text-transform: uppercase; }
.card { background: #FFFFFF; border: 1px solid #E6E6E6; border-top: 0; margin-bottom: 32px; padding: 26px 28px 20px 28px; box-sizing: border-box; }
.kicker { color: #DF212A; font-size: 11px; font-weight: bold; letter-spacing: 1.5px; text-transform: uppercase; margin: 0 0 4px 0; }
h1 { font-size: 22px; font-weight: bold; margin: 0 0 14px 0; }
h2 { font-size: 12px; font-weight: bold; letter-spacing: 1.2px; text-transform: uppercase; color: #262626; margin: 0 0 12px 0; padding-bottom: 6px; border-bottom: 1px solid #E6E6E6; }
section { margin-top: 26px; }
p, td, label, legend { font-size: 14px; line-height: 21px; }
.hint { color: #707070; font-size: 13px; margin: 0 0 10px 0; }
.muted { color: #707070; }
.req { color: #DF212A; }
.error { color: #A1161D; font-weight: bold; margin: 8px 0; }
.ok { color: #1B6E2E; font-weight: bold; margin: 8px 0; }
.notice { background: #FAFAFA; border-left: 4px solid #DF212A; padding: 10px 14px; margin: 14px 0; color: #262626; }
label { display: block; margin: 0 0 14px 0; font-size: 13px; color: #262626; }
input[type=text], input:not([type]), input[type=email], input[type=password], input[type=url], textarea, select {
  display: block; width: 100%; max-width: 100%; box-sizing: border-box; margin-top: 5px; padding: 9px 10px;
  font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #262626; background: #FFFFFF; border: 1px solid #BDBDBD; border-radius: 0;
}
input:focus, textarea:focus, select:focus { outline: 2px solid #DF212A; outline-offset: -1px; border-color: #DF212A; }
input[type=checkbox] { width: auto; margin: 0 8px 0 0; vertical-align: middle; }
label.check { display: flex; align-items: center; margin: 0 0 8px 0; font-size: 14px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 22px; }
.options { display: grid; grid-template-columns: 1fr 1fr; gap: 2px 22px; margin-bottom: 14px; }
.actions { margin-top: 26px; padding-top: 18px; border-top: 1px solid #E6E6E6; }
button { background: #DF212A; color: #FFFFFF; border: 0; padding: 10px 22px; font-family: Arial, Helvetica, sans-serif; font-size: 13px; font-weight: bold; letter-spacing: 0.5px; text-transform: uppercase; cursor: pointer; }
button.secondary { background: #262626; font-size: 12px; padding: 7px 12px; text-transform: none; letter-spacing: 0; }
button.ghost { background: transparent; color: #DF212A; border: 1px solid #DF212A; font-size: 12px; padding: 6px 12px; text-transform: none; letter-spacing: 0; }
a.button { display: inline-block; background: #DF212A; color: #FFFFFF; padding: 10px 22px; text-decoration: none; font-weight: bold; font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }
a { color: #DF212A; }
form.inline { display: inline-block; margin: 0 6px 6px 0; }
.nav { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; border-bottom: 1px solid #E6E6E6; padding-bottom: 12px; margin-bottom: 18px; font-size: 13px; color: #707070; }
.nav .links a { margin-left: 12px; font-weight: bold; text-decoration: none; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; color: #707070; font-weight: bold; border-bottom: 2px solid #262626; padding: 8px 10px 8px 0; }
td { border-bottom: 1px solid #E6E6E6; padding: 9px 10px 9px 0; vertical-align: top; }
pre { background: #F7F7F7; border: 1px solid #E6E6E6; padding: 10px; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
.footer { color: #707070; font-size: 11px; border-top: 1px solid #E6E6E6; margin-top: 28px; padding-top: 12px; }
form.rowform { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 8px 0; }
form.rowform select, form.rowform input { display: inline-block; width: auto; margin: 0; padding: 6px 8px; font-size: 13px; }
form.rowform button { padding: 7px 12px; font-size: 12px; text-transform: none; letter-spacing: 0; }
button.danger { background: #A1161D; }
@media (max-width: 640px) { .grid-2, .options { grid-template-columns: 1fr; } .card { padding: 20px 16px; } }
"""


@lru_cache(maxsize=1)
def _logo_data_uri(path: str) -> str:
    file = Path(path)
    if not file.is_file():
        return ""
    return "data:image/png;base64," + base64.b64encode(file.read_bytes()).decode("ascii")


ROUTE_PREFIXES = ("/m/", "/p/", "/a/", "/u/")
ROUTE_EXACT = ("/p", "/a", "/logout", "/login")
MESSAGES = {
    "saved": "Saved.",
    "activated": "Saved and activated: the next weekly feed includes this client.",
}


def is_personal_path(path: str) -> bool:
    return path in ROUTE_EXACT or path.startswith(ROUTE_PREFIXES)


class PersonalWeb:
    def __init__(self, cfg: Config, base_url: str) -> None:
        self.cfg = cfg
        self.base = base_url
        self.settings: PersonalSettings = cached_settings(cfg.root)
        self.secure_cookie = base_url.startswith("https://")

    # Entry point ------------------------------------------------------------------------------

    def handle(self, handler, method: str) -> None:
        path = urlsplit(handler.path).path
        length = int(handler.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._page(handler, 413, "Request too large", "<p>That form was too large.</p>")
        form = parse_qs(handler.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True) if length else {}
        if method == "POST" and not _same_origin(handler.headers):
            return self._page(handler, 403, "Refused", "<p>The form did not come from this site.</p>")

        conn = store.connect(self.cfg.db_path)
        try:
            now = datetime.now(timezone.utc)
            self._route(handler, method, path, form, conn, now)
        finally:
            conn.close()

    def _route(self, handler, method, path, form, conn, now) -> None:
        if path == "/login":
            return self._shared_login(handler, method, form, conn, now)
        if path.startswith("/m/"):
            return self._sign_in(handler, method, path[3:], conn, now)
        if path.startswith("/u/"):
            return self._unsubscribe(handler, method, path[3:], conn, now)
        if path == "/logout" and method == "POST":
            return self._logout(handler, form, conn, now)

        person = store.session_for(conn, _cookie(handler), now)
        if person is None:
            if method == "GET" and path.startswith(("/a", "/p")):
                return self._redirect(handler, "/login")
            return self._page(handler, 401, "Sign in", "<p>Open the sign-in link from your email to continue. "
                              "If it has expired, ask an admin for a new one.</p>")
        if path in ("/p", "/p/") and method == "GET":
            return self._my_clients(handler, person, conn)
        if path == "/p/request" and method == "POST":
            return self._request_approval(handler, person, form, conn, now)
        if path == "/p/clients/new" and method == "GET":
            return self._form(handler, person, conn, None, {}, [], is_new=True)
        if path == "/p/clients" and method == "POST":
            return self._save_client(handler, person, form, conn, now)
        if path.startswith("/p/clients/"):
            return self._client_route(handler, method, path, person, form, conn, now)
        if path in ("/a", "/a/") and method == "GET":
            return self._admin_home(handler, person, conn, now)
        if path == "/a/password" and method == "POST":
            return self._change_password(handler, person, form, conn, now)
        if path.startswith("/a/") and method == "POST":
            return self._admin_action(handler, person, path, form, conn, now)
        return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)

    # Sign-in ----------------------------------------------------------------------------------

    def _sign_in(self, handler, method, raw, conn, now) -> None:
        if method == "GET":
            if not raw:
                return self._invalid(handler)
            return self._page(handler, 200, "Sign in", f"""<p>Sign in to set up the weekly client feeds.</p>
<form method="post" action="/m/{_e(raw)}"><button>Sign in</button></form>""")
        person = store.redeem_invite(conn, raw, now)
        if person is None:
            return self._invalid(handler)
        token, _ = store.create_session(conn, person["id"], now, self.settings.session_days)
        target = "/a/" if person["role"] == "admin" else "/p/"
        self._redirect(handler, target, cookie=self._cookie_header(token, self.settings.session_days))

    def _shared_login(self, handler, method, form, conn, now) -> None:
        error = ""
        if method == "POST":
            username = (_field(form, "username") or "").strip()
            password = _field(form, "password") or ""
            if store.check_shared_login(conn, username, password):
                admin = store.ensure_shared_admin(conn, now)
                token, _ = store.create_session(conn, admin["id"], now, self.settings.session_days)
                return self._redirect(handler, "/a/", cookie=self._cookie_header(token, self.settings.session_days))
            time.sleep(1)  # slows down guessing
            error = "The username or password is not correct."
        body = f"""<p class="kicker">Admin sign-in</p>
<h1>Sign in to the admin dashboard</h1>
{f'<p class="error">{_e(error)}</p>' if error else ''}
<form method="post" action="/login" class="stack" style="max-width: 420px;">
<label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input type="password" name="password" autocomplete="current-password" required></label>
<div class="actions"><button>Sign in</button></div>
</form>
<p class="hint">Managers sign in from the link in their email.</p>"""
        self._page(handler, 200, "Sign in", body)

    def _change_password(self, handler, person, form, conn, now) -> None:
        if not _csrf_ok(form, person):
            return self._page(handler, 403, "Refused", "<p>Your session has expired.</p>", person=person)
        current = _field(form, "current") or ""
        new = _field(form, "new") or ""
        confirm = _field(form, "confirm") or ""
        username = store.shared_username(conn)
        if not store.check_shared_login(conn, username, current):
            notice = "The current password is not correct."
        elif new != confirm:
            notice = "The new passwords do not match."
        else:
            try:
                store.set_shared_password(conn, new)
            except ValueError as exc:
                notice = str(exc)
            else:
                store.audit(conn, person["email"], "password.change", username, "shared admin login", now=now)
                return self._admin_home(handler, person, conn, now,
                                        notice_html='<p class="ok">The password was changed. Use the new one next time.</p>')
        return self._admin_home(handler, person, conn, now, notice_html=f'<p class="error">{_e(notice)}</p>')

    def _logout(self, handler, form, conn, now) -> None:
        raw = _cookie(handler)
        person = store.session_for(conn, raw, now)
        if person is None or not _csrf_ok(form, person):
            return self._page(handler, 403, "Refused", "<p>Your session has expired. Sign in again.</p>")
        store.end_session(conn, raw, now)
        self._redirect(handler, "/", cookie=f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
                       + ("; Secure" if self.secure_cookie else ""))

    def _cookie_header(self, token: str, days: int) -> str:
        return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={days * 86400}" + (
            "; Secure" if self.secure_cookie else ""
        )

    def _invalid(self, handler) -> None:
        self._page(handler, 400, "Link not valid", """<p>This sign-in link has expired or was already used.
Ask an admin to send you a new one.</p>""")

    # Manager pages ----------------------------------------------------------------------------

    def _my_clients(self, handler, person, conn, notice: str | None = None) -> None:
        rows = store.clients_of(conn, person["id"])
        items = "".join(_client_row(row, conn) for row in rows) or '<tr><td class="muted">No clients yet.</td></tr>'
        if store.is_approved(person):
            approval = '<p class="ok">Your account is approved. You can activate clients.</p>'
        elif store.pending_request_for(conn, person["id"]) is not None:
            approval = ('<p class="notice">Waiting for an admin to approve your account. You can keep drafting clients; '
                        "activation opens once you are approved.</p>")
        else:
            approval = """<p class="notice">Your clients stay as drafts until an admin approves your account.</p>
<form method="post" action="/p/request"><input type="hidden" name="csrf" value="{csrf}">
<button>Submit for approval</button></form>""".replace("{csrf}", _e(person["csrf"]))
        notice_html = f"<p class='ok'>{_e(notice)}</p>" if notice else ""
        body = f"""<h2>Your clients</h2>
{notice_html}{approval}
<p><a class="button" href="/p/clients/new">Add a client</a></p>
<table>{items}</table>"""
        self._page(handler, 200, "Your clients", body, person=person)

    def _request_approval(self, handler, person, form, conn, now) -> None:
        if not _csrf_ok(form, person):
            return self._page(handler, 403, "Refused", "<p>Your session has expired. Reload the page.</p>", person=person)
        if store.is_approved(person):
            return self._redirect(handler, "/p/")
        clients = store.clients_of(conn, person["id"])
        if not clients:
            return self._my_clients(handler, person, conn, notice="Add at least one client before you submit for approval.")
        request_id = store.submit_request(conn, person["id"], now)
        if request_id is None:
            return self._my_clients(handler, person, conn, notice="Your request is already waiting for an admin.")
        names = ", ".join(row["client_name"] for row in clients)
        store.audit(conn, person["email"], "request.submit", str(request_id), names, now=now)

        def build(link: str, admin) -> tuple[str, str, str]:
            return mail.notice(
                self.cfg,
                subject=f"Approval needed: {person['email']}",
                heading="A manager is waiting for approval",
                paragraphs=[f"{person['email']} has set up {len(clients)} client(s): {names}.",
                            "Review the clients and approve or reject the request. The first decision counts."],
                button=("Review the request", link),
            )

        notify.notify_people(conn, self.cfg, self.settings, self.base, now, notify.admins(conn), build)
        self._my_clients(handler, person, conn, notice="Submitted. The admins have been emailed. You can keep editing drafts.")

    def _client_route(self, handler, method, path, person, form, conn, now) -> None:
        parts = path.split("/")  # "", "p", "clients", id, [action]
        if len(parts) < 4 or not parts[3].isdigit():
            return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)
        row = store.client(conn, int(parts[3]))
        if row is None or not _may_edit(person, row):
            return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)
        action = parts[4] if len(parts) > 4 else ""
        if method == "GET" and action == "":
            message = MESSAGES.get(parse_qs(urlsplit(handler.path).query).get("msg", [""])[0])
            return self._form(handler, person, conn, row, _row_fields(row), [], is_new=False, notice=message)
        if method == "GET" and action == "preview":
            return self._preview(handler, row, conn, now)
        if method == "POST" and action == "recipients-remove":
            if not _csrf_ok(form, person):
                return self._page(handler, 403, "Refused", "<p>Your session has expired.</p>", person=person)
            address = (_field(form, "address") or "").strip().lower()
            store.remove_recipient(conn, row["id"], address)
            store.audit(conn, person["email"], "team.remove", row["client_name"], address, now=now)
            return self._redirect(handler, f"/p/clients/{row['id']}?msg=saved")
        if method == "POST" and action == "":
            return self._save_client(handler, person, form, conn, now, client_id=row["id"])
        return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)

    def _form(self, handler, person, conn, row, fields, errors, *, is_new, notice: str | None = None) -> None:
        industries = self.settings.industries
        checked = set(fields.get("industries", []))
        ind_boxes = "".join(
            f'<label class="check"><input type="checkbox" name="industries" value="{_e(i.id)}"'
            f'{" checked" if i.id in checked else ""}> {_e(i.title)}</label>'
            for i in industries.values()
        )
        custom_value = _one(fields, "custom_industry") or next(
            (item[len(profiles.CUSTOM_PREFIX):] for item in fields.get("industries", []) if item.startswith(profiles.CUSTOM_PREFIX)),
            "",
        )
        suggested = json.loads(row["suggested_json"]) if row is not None else (fields.get("_suggested") or [])
        suggested_note = ""
        if suggested and (row is None or row["reviewed_at"] is None):
            suggested_note = f"""<div class="notice">The engine pre-filled some fields from your industries:
{_e(', '.join(suggested))}. Check them, then tick the confirmation below. Until you confirm, the client is not sent.
<label class="check"><input type="checkbox" name="reviewed" value="1"> I have reviewed the suggested values</label></div>"""
        approval_note = "" if store.is_approved(person) else (
            '<div class="notice">Your account is waiting for admin approval. You can save drafts now; '
            "activation opens once you are approved.</div>"
        )
        error_html = "".join(f"<p class='error'>{_e(item)}</p>" for item in errors)
        notice_html = f"<p class='ok'>{_e(notice)}</p>" if notice else ""
        recipients = store.recipients_of(conn, row["id"]) if row is not None else []
        recipients_html = "".join(
            f"""<tr><td>{_e(r['address'])}</td><td>{_e(r['status'])}</td><td>
<form method="post" action="/p/clients/{row['id']}/recipients-remove"><input type="hidden" name="csrf" value="{_e(person['csrf'])}">
<input type="hidden" name="address" value="{_e(r['address'])}"><button class="secondary">Remove</button></form></td></tr>"""
            for r in recipients
        ) if row is not None else ""
        action_path = "/p/clients" if is_new else f"/p/clients/{row['id']}"
        saved = "" if is_new else f'<p><a href="/p/clients/{row["id"]}/preview" target="_blank">Preview this week\'s feed</a></p>'
        day_now = _one(fields, "send_day") or "0"
        day_options = "".join(
            f'<option value="{index}"{" selected" if str(index) == day_now else ""}>{name}</option>'
            for index, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))
        )
        body = f"""<p class="kicker">Client profile</p>
<h1>{'New client' if is_new else _e(_one(fields, 'client_name') or 'Client')}</h1>
{notice_html}{error_html}{approval_note}{suggested_note}
<form method="post" action="{action_path}" class="stack">
<input type="hidden" name="csrf" value="{_e(person['csrf'])}">
<section>
  <h2>1. Client</h2>
  <div class="grid-2">
    <label>Client name <span class="req">*</span><input name="client_name" value="{_e(_one(fields, 'client_name'))}" maxlength="120" required></label>
    <label>Website<input name="website" value="{_e(_one(fields, 'website'))}" placeholder="https://example.com"></label>
  </div>
  <label>Other names, one per line<textarea name="aliases" rows="2">{_e(_one(fields, 'aliases'))}</textarea></label>
</section>
<section>
  <h2>2. Operating industries <span class="req">*</span></h2>
  <p class="hint">Select each line of business that applies, or type one that is not listed.</p>
  <div class="options">{ind_boxes}</div>
  <label>Other industry<input name="custom_industry" value="{_e(custom_value)}" maxlength="80" placeholder="e.g. Logistics &amp; 3PL"></label>
</section>
<section>
  <h2>3. Competitors and keywords</h2>
  <label>Competitors, one per line (Name | website)<textarea name="competitors" rows="5">{_e(_one(fields, 'competitors'))}</textarea></label>
  <div class="grid-2">
    <label>Keywords to follow, one per line<textarea name="keywords" rows="4">{_e(_one(fields, 'keywords'))}</textarea></label>
    <label>Words to exclude, one per line<textarea name="excludes" rows="4">{_e(_one(fields, 'excludes'))}</textarea></label>
  </div>
</section>
<section>
  <h2>4. Weekly feed recipients</h2>
  <label>Day of the week the feed arrives (once a week)<select name="send_day">{day_options}</select></label>
  <label>Team members, one per line or separated by commas<textarea name="recipients" rows="4" placeholder="name@rankuno.com">{_e(_one(fields, 'recipients'))}</textarea></label>
  <label class="check"><input type="checkbox" name="activate" value="1"> Activate: send this client's feed every Monday</label>
</section>
<div class="actions"><button name="action" value="save">Save client</button></div>
</form>
{saved}
{_recipients_table(recipients_html) if row is not None else ''}"""
        self._page(handler, 200, "Client", body, person=person)

    def _save_client(self, handler, person, form, conn, now, client_id: int | None = None) -> None:
        existing = store.client(conn, client_id) if client_id else None
        if existing is not None and not _may_edit(person, existing):
            return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)
        if not _csrf_ok(form, person):
            return self._page(handler, 403, "Refused", "<p>Your session has expired. Reload the page.</p>", person=person)

        data, errors, extra = profiles.parse_form(form, self.settings, is_new=existing is None)
        if data is None:
            fields = {key: list(values) for key, values in form.items()}
            return self._form(handler, person, conn, existing, fields, errors, is_new=existing is None)

        if existing is not None:
            # Keep the engine's suggestions until the manager confirms them; editing must not clear the review.
            data["suggested"] = json.loads(existing["suggested_json"])
        addresses, rejected = profiles.parse_addresses(extra.get("text", ""))
        with DnsResolver() as resolver:
            check = check_recipients(addresses, self.cfg.security.recipients, suppressed=db.suppressed_addresses(conn), resolver=resolver)
        notes = [f"Not added: {entry} ({reason})" for entry, reason in rejected + check.rejected]

        ticked = _field(form, "reviewed") == "1" and bool(data["suggested"])
        if existing is None:
            client_id = store.create_client(conn, person["id"], data, now)
            if ticked:
                store.mark_reviewed(conn, client_id, now)
        else:
            client_id = existing["id"]
            store.update_client(conn, client_id, data, now, reviewed=ticked)
        store.add_recipients(conn, client_id, check.accepted, now)
        row = store.client(conn, client_id)
        store.audit(conn, person["email"], "client.create" if existing is None else "client.update",
                    row["client_name"], f"client {client_id}", now=now)

        activated = False
        if _field(form, "activate") == "1" and row["status"] != "paused":
            reviewed = row["reviewed_at"] is not None or not data["suggested"]
            has_team = bool(store.active_recipient_rows(conn, client_id))
            if not store.is_approved(person):
                notes.append("Your account is waiting for admin approval, so this client is saved as a draft.")
            elif not reviewed:
                notes.append("Confirm the suggested values (tick the box) to activate. Saved as a draft.")
            elif not has_team:
                notes.append("Add at least one team member to activate. Saved as a draft.")
            else:
                store.set_client_status(conn, client_id, "active", now)
                store.audit(conn, person["email"], "client.activate", row["client_name"], now=now)
                activated = True

        if existing is not None and person["role"] != "admin" and row["status"] == "active":
            self._notify_edit(conn, person, row, now)
        if notes:
            submitted = {key: list(values) for key, values in form.items()}  # keep the manager's unsaved typing
            return self._form(handler, person, conn, store.client(conn, client_id), submitted, notes, is_new=False)
        return self._redirect(handler, f"/p/clients/{client_id}?msg={'activated' if activated else 'saved'}")

    def _notify_edit(self, conn, person, row, now) -> None:
        """A manager changed an active client: the admins are told, with a sign-in link to the admin page."""
        def build(link: str, admin) -> tuple[str, str, str]:
            return mail.notice(
                self.cfg,
                subject=f"Client changed: {row['client_name']}",
                heading="A client feed was changed",
                paragraphs=[f"{person['email']} changed {row['client_name']}.",
                            "Open the admin page to review the change. Everything is in the audit log."],
                button=("Open the admin page", link),
            )

        notify.notify_people(conn, self.cfg, self.settings, self.base, now, notify.admins(conn), build)

    def _preview(self, handler, row, conn, now) -> None:
        pool = feed_runner.screened_news(self.cfg, conn, now - timedelta(days=self.settings.window_days))
        built = feed_runner.build_one(conn, self.settings, pool, row, now, dedupe=False)
        if not built.matches:
            return self._page(handler, 200, "Preview", "<p>No matching news in the last week.</p>")
        subject, body_html, _ = feed_runner.message_for(self.cfg, self.settings, [built], now=now, unsub_links=None)
        logo = mail.logo_inline(self.cfg)["logo_white"]
        if logo.is_file():
            data = base64.b64encode(logo.read_bytes()).decode("ascii")
            body_html = body_html.replace('"cid:logo_white"', f'"data:image/png;base64,{data}"')
        payload = body_html.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Content-Security-Policy", PREVIEW_CSP)
        handler._security_headers()
        handler.end_headers()
        handler.wfile.write(payload)

    # Admin ------------------------------------------------------------------------------------

    def _admin_home(self, handler, person, conn, now, notice_html: str = "") -> None:
        if person["role"] != "admin":
            return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)
        csrf = _e(person["csrf"])
        requests_rows = "".join(
            f"""<tr><td>{_e(r['manager_email'])}</td><td>{self._when(r['submitted_at'])}</td>
<td>{_e(', '.join(c['client_name'] for c in store.clients_of(conn, r['manager_id'])) or 'no clients')}</td>
<td><form method="post" action="/a/requests/{r['id']}/approve" class="inline"><input type="hidden" name="csrf" value="{csrf}">
<button>Approve</button></form>
<form method="post" action="/a/requests/{r['id']}/reject" class="inline"><input type="hidden" name="csrf" value="{csrf}">
<input name="reason" placeholder="Reason" required maxlength="500"> <button class="secondary">Reject</button></form></td></tr>"""
            for r in store.pending_requests(conn)
        )
        people_rows = "".join(
            f"""<tr><td>{_e(p['email'])}</td><td>{_e(p['name'])}</td><td>{_e(p['role'])}</td>
<td>{'active' if p['active'] else 'deactivated'}{' · approved' if p['approved_at'] else ' · waiting for approval' if p['role'] == 'manager' else ''}</td>
<td><form method="post" action="/a/people/{p['id']}/invite"><input type="hidden" name="csrf" value="{csrf}"><button class="secondary">New sign-in link</button></form></td></tr>"""
            for p in store.people(conn)
        )
        choices = [p for p in store.people(conn) if p["role"] in ("manager", "admin") and p["active"] and not p["shared"]]
        client_rows = []
        for c in store.clients_of(conn):
            owner = store.person(conn, c["manager_id"])
            menu = list(choices)
            if owner is not None and owner["id"] not in [p["id"] for p in menu]:
                menu.insert(0, owner)  # the current owner always shows, even if the menu would not list them
            options = "".join(
                f'<option value="{p["id"]}"{" selected" if p["id"] == c["manager_id"] else ""}>{_e(p["email"])}</option>'
                for p in menu
            )
            count = len(store.active_recipient_rows(conn, c["id"]))
            pending = "review pending" if c["suggested_json"] != "[]" and c["reviewed_at"] is None else ""
            client_rows.append(
                f"""<tr><td><a href="/p/clients/{c['id']}">{_e(c['client_name'])}</a></td><td>{_e(owner['email'] if owner else '')}</td>
<td>{_e(c['status'])} {pending}</td><td>{count}</td>
<td>
<form method="post" action="/a/clients/{c['id']}/manager" class="rowform"><input type="hidden" name="csrf" value="{csrf}">
<select name="manager_id" aria-label="Owner">{options}</select><button class="secondary">Reassign</button></form>
<form method="post" action="/a/clients/{c['id']}/status" class="rowform"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="status" value="{'paused' if c['status'] == 'active' else 'active'}">
<button class="secondary">{'Pause' if c['status'] == 'active' else 'Resume'}</button></form>
<form method="post" action="/a/clients/{c['id']}/delete" class="rowform"><input type="hidden" name="csrf" value="{csrf}">
<input type="password" name="password" placeholder="Shared admin password" autocomplete="current-password" required>
<button class="danger">Delete</button></form>
</td></tr>"""
            )
        audit_rows = "".join(
            f"<tr><td>{self._when(a['at'])}</td><td>{_e(a['actor'])}</td><td>{_e(a['action'])}</td>"
            f"<td>{_e(a['target'])}</td><td>{_e(a['detail'])}</td></tr>"
            for a in store.recent_audit(conn)
        )
        run = store.run_for_date(conn, now.astimezone(self.cfg.newsletter.timezone).date().isoformat())
        body = f"""<h2>Admin</h2>
{notice_html}
<p>Weekly feed: <strong>{'ON' if personal_sending_enabled() else 'OFF'}</strong> (PERSONAL_SEND_ENABLED).
Today's run: {_e(run['status']) if run else 'not run yet'}.</p>
<h2>Shared admin login</h2>
<p class="hint">Username <strong>{_e(store.shared_username(conn))}</strong>. Share the password only with the three admins.</p>
<form method="post" action="/a/password" class="stack">
<input type="hidden" name="csrf" value="{csrf}">
<div class="grid-2">
<label>Current password<input type="password" name="current" autocomplete="current-password" required></label>
<label>New password (at least 12 characters)<input type="password" name="new" autocomplete="new-password" minlength="12" required></label>
</div>
<label>Repeat the new password<input type="password" name="confirm" autocomplete="new-password" minlength="12" required></label>
<div class="actions"><button class="secondary">Change password</button></div>
</form>
<h2>Approval requests</h2>
<table>{requests_rows or '<tr><td class="muted">No requests waiting.</td></tr>'}</table>
<h2>Invite a manager or admin</h2>
<form method="post" action="/a/people" class="stack">
<input type="hidden" name="csrf" value="{csrf}">
<label>Email (rankuno.com)<input name="email" required></label>
<label>Name<input name="name"></label>
<label>Role<select name="role"><option value="manager">Manager</option><option value="admin">Admin</option></select></label>
<div class="actions"><button>Create sign-in link</button></div>
</form>
<h2>People</h2><table>{people_rows}</table>
<h2>Clients</h2><table>{''.join(client_rows) or '<tr><td class="muted">No clients yet.</td></tr>'}</table>
<h2>Audit log (latest 40)</h2>
<table>{audit_rows or '<tr><td class="muted">Nothing yet.</td></tr>'}</table>"""
        self._page(handler, 200, "Admin", body, person=person)

    def _admin_action(self, handler, person, path, form, conn, now) -> None:
        if person["role"] != "admin":
            return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)
        if not _csrf_ok(form, person):
            return self._page(handler, 403, "Refused", "<p>Your session has expired.</p>", person=person)
        if path == "/a/people":
            return self._invite(handler, person, form, conn, now)
        parts = path.split("/")  # "", "a", "people"|"clients"|"requests", id, action
        if len(parts) == 5 and parts[2] == "requests" and parts[3].isdigit() and parts[4] in ("approve", "reject"):
            return self._decide(handler, person, int(parts[3]), parts[4] == "approve", form, conn, now)
        if len(parts) == 5 and parts[2] == "people" and parts[3].isdigit() and parts[4] == "invite":
            target = store.person(conn, int(parts[3]))
            if target is None or not target["active"]:
                return self._redirect(handler, "/a/")
            raw = store.create_invite(conn, target["id"], person["email"], now, self.settings.invite_days)
            store.audit(conn, person["email"], "invite.new_link", target["email"], now=now)
            return self._show_link(handler, person, conn, now, target["email"], target["name"], raw)
        if len(parts) == 5 and parts[2] == "clients" and parts[3].isdigit():
            row = store.client(conn, int(parts[3]))
            if row is None:
                return self._redirect(handler, "/a/")
            if parts[4] == "delete":
                return self._delete_client(handler, person, row, form, conn, now)
            if parts[4] == "manager":
                manager = store.person(conn, int(_field(form, "manager_id") or 0))
                if manager is not None and manager["role"] in ("manager", "admin") and manager["active"]:
                    store.reassign_client(conn, row["id"], manager["id"], now)
                    store.audit(conn, person["email"], "client.reassign", row["client_name"], manager["email"], now=now)
            elif parts[4] == "status" and _field(form, "status") in ("active", "paused"):
                store.set_client_status(conn, row["id"], _field(form, "status"), now)
                store.audit(conn, person["email"], f"client.{_field(form, 'status')}", row["client_name"], now=now)
            return self._redirect(handler, "/a/")
        return self._page(handler, 404, "Not found", "<p>Not found.</p>", person=person)

    def _delete_client(self, handler, person, row, form, conn, now) -> None:
        password = _field(form, "password") or ""
        if not store.check_shared_login(conn, store.shared_username(conn), password):
            time.sleep(1)  # slows down guessing
            return self._admin_home(handler, person, conn, now,
                                    notice_html='<p class="error">The shared admin password is not correct. Nothing was deleted.</p>')
        name = row["client_name"]
        store.delete_client(conn, row["id"])
        store.audit(conn, person["email"], "client.delete", name, f"client {row['id']}", now=now)
        return self._admin_home(handler, person, conn, now, notice_html=f'<p class="ok">{_e(name)} was deleted.</p>')

    def _decide(self, handler, person, request_id: int, approve: bool, form, conn, now) -> None:
        reason = (_field(form, "reason") or "").strip()[:500]
        if not approve and not reason:
            return self._admin_home(handler, person, conn, now, notice_html='<p class="error">Give a reason for rejecting.</p>')
        row = store.decide_request(conn, request_id, approve=approve, by=person["email"], reason=reason, now=now)
        if row is None:
            return self._admin_home(handler, person, conn, now, notice_html='<p class="notice">This request was already decided.</p>')
        verb = "approved" if approve else "rejected"
        store.audit(conn, person["email"], f"request.{verb}", row["manager_email"], reason, now=now)

        manager = store.person(conn, row["manager_id"])
        if approve:
            manager_paragraphs = ["Your account is approved. Sign in to activate your clients.",
                                  "Each client's team members receive the weekly feed only after you activate it."]
        else:
            manager_paragraphs = [f"Your request was not approved: {reason}", "Sign in to update your clients and submit again."]

        def to_manager(link: str, _person) -> tuple[str, str, str]:
            return mail.notice(self.cfg, subject=f"Your client feed account: {verb}", heading=f"Request {verb}",
                               paragraphs=manager_paragraphs, button=("Sign in", link))

        notify.notify_people(conn, self.cfg, self.settings, self.base, now, [manager], to_manager)

        def to_admins(link: str, _admin) -> tuple[str, str, str]:
            return mail.notice(self.cfg, subject=f"Request {verb}: {row['manager_email']}", heading=f"Request {verb}",
                               paragraphs=[f"{person['email']} {verb} the request from {row['manager_email']}."],
                               button=("Open the admin page", link))

        notify.notify_people(conn, self.cfg, self.settings, self.base, now, notify.admins(conn), to_admins)
        return self._admin_home(handler, person, conn, now, notice_html=f'<p class="ok">Request {verb}.</p>')

    def _invite(self, handler, person, form, conn, now) -> None:
        address = normalize_address((_field(form, "email") or "").strip())
        role = _field(form, "role") or "manager"
        if address is None or role not in ("manager", "admin"):
            return self._admin_home(handler, person, conn, now, notice_html='<p class="error">Enter a valid email address.</p>')
        with DnsResolver() as resolver:
            check = check_recipients([address], self.cfg.security.recipients, resolver=resolver)
        if not check.accepted:
            reason = check.rejected[0][1] if check.rejected else "not allowed"
            return self._admin_home(handler, person, conn, now, notice_html=f'<p class="error">{_e(address)}: {_e(reason)}</p>')
        name = (_field(form, "name") or "").strip()[:120]
        person_id = store.add_person(conn, address, name, role, now)
        if role == "admin":
            store.approve_person(conn, person_id, now)
        raw = store.create_invite(conn, person_id, person["email"], now, self.settings.invite_days)
        store.audit(conn, person["email"], "invite.create", address, role, now=now)
        return self._show_link(handler, person, conn, now, address, name, raw)

    def _show_link(self, handler, person, conn, now, address, name, raw) -> None:
        link = f"{self.base}/m/{raw}"
        if personal_sending_enabled():
            subject, html_body, text_body = mail.invite_subject(self.cfg), *mail.invite_email(self.cfg, name, link, self.settings.invite_days)
            if notify.send(self.cfg, address, subject, html_body, text_body):
                note = f"A sign-in link was emailed to {address}. It works once and expires in {self.settings.invite_days} days."
                return self._admin_home(handler, person, conn, now, notice_html=f'<p class="ok">{_e(note)}</p>')
            note = "The email could not be sent. Copy the link below and send it yourself."
        else:
            note = "Email sending is off, so no email was sent. Copy the link below and send it yourself."
        body_notice = f"""<p class="notice">{_e(note)}</p>
<p>Sign-in link for <strong>{_e(address)}</strong> (shown once; it works once and expires in {self.settings.invite_days} days):</p>
<pre>{_e(link)}</pre>"""
        self._admin_home(handler, person, conn, now, notice_html=body_notice)

    def _when(self, value: str) -> str:
        return db.from_iso(value).astimezone(self.cfg.newsletter.timezone).strftime("%d %b %Y, %H:%M")

    # Unsubscribe ------------------------------------------------------------------------------

    def _unsubscribe(self, handler, method, raw, conn, now) -> None:
        if method == "GET":
            row = conn.execute(
                "SELECT r.address, c.client_name FROM personal_recipients r JOIN personal_clients c ON c.id = r.client_id WHERE r.unsub_token = ?",
                (raw,),
            ).fetchone()
            if row is None:
                return self._page(handler, 404, "Link not valid", "<p>This unsubscribe link is not valid.</p>")
            return self._page(handler, 200, "Unsubscribe", f"""<p>Stop sending <strong>{_e(row['client_name'])}</strong> news to {_e(row['address'])}?</p>
<form method="post" action="/u/{_e(raw)}"><button>Unsubscribe</button></form>""")
        row = store.unsubscribe(conn, raw, now)
        if row is None:
            return self._page(handler, 404, "Link not valid", "<p>This unsubscribe link is not valid.</p>")
        return self._page(handler, 200, "Unsubscribed", f"<p>You will no longer receive {_e(row['client_name'])} news.</p>")

    # Output -----------------------------------------------------------------------------------

    def _page(self, handler, status: int, title: str, body: str, *, person=None) -> None:
        nav = ""
        if person is not None:
            csrf = _e(person["csrf"])
            links = '<a href="/p/">My clients</a>' + (' <a href="/a/">Admin</a>' if person["role"] == "admin" else "")
            nav = (f'<div class="nav"><span>Signed in as {_e(person["email"])}</span>'
                   f'<span class="links">{links}</span>'
                   f'<form method="post" action="/logout" class="inline"><input type="hidden" name="csrf" value="{csrf}">'
                   '<button class="ghost">Sign out</button></form></div>')
        logo = _logo_data_uri(str(mail.logo_inline(self.cfg)["logo_white"]))
        logo_html = f'<img src="{logo}" alt="RankUno" height="34">' if logo else ""
        document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · Client feeds</title>
<style>{STYLE}</style></head><body>
<header class="masthead"><div class="wrap">{logo_html}<div class="product">The RankUno Brief · Client feeds</div></div></header>
<main class="wrap card">
{nav}
{body}
<p class="footer">Prepared for internal circulation within RankUno. Client feeds are separate from the general brief.</p>
</main></body></html>"""
        payload = document.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Content-Security-Policy", CSP)
        handler._security_headers()
        handler.end_headers()
        handler.wfile.write(payload)

    def _redirect(self, handler, location: str, *, cookie: str | None = None) -> None:
        handler.send_response(303)
        handler.send_header("Location", location)
        if cookie:
            handler.send_header("Set-Cookie", cookie)
        handler._security_headers()
        handler.send_header("Content-Length", "0")
        handler.end_headers()


# Helpers --------------------------------------------------------------------------------------


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _one(fields: dict, name: str) -> str:
    values = fields.get(name) or [""]
    return values[0] if isinstance(values, list) else str(values)


def _field(form: dict, name: str) -> str | None:
    values = form.get(name)
    return values[0] if values else None


def _csrf_ok(form: dict, person) -> bool:
    supplied = _field(form, "csrf") or ""
    return bool(person) and hmac.compare_digest(supplied, person["csrf"])


def _may_edit(person, row) -> bool:
    return person["role"] == "admin" or row["manager_id"] == person["id"]


def _cookie(handler) -> str | None:
    header = handler.headers.get("Cookie")
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # noqa: BLE001 - a malformed cookie is simply not a session
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


def _same_origin(headers) -> bool:
    source = headers.get("Origin") or headers.get("Referer")
    return bool(source) and source != "null" and urlsplit(source).netloc == headers.get("Host")


def _row_fields(row: sqlite3.Row) -> dict:
    """Form values for an existing client, as the form shows them."""
    competitors = json.loads(row["competitors_json"])
    return {
        "client_name": [row["client_name"]],
        "website": [row["website"]],
        "aliases": ["\n".join(json.loads(row["aliases_json"]))],
        "industries": json.loads(row["industries_json"]),
        "competitors": ["\n".join(f"{c['name']} | {c['website']}".rstrip(" |") for c in competitors)],
        "keywords": ["\n".join(json.loads(row["keywords_json"]))],
        "excludes": ["\n".join(json.loads(row["excludes_json"]))],
        "recipients": [""],
        "send_day": [str(row["send_day"])],
        "custom_industry": [next((i[len(profiles.CUSTOM_PREFIX):] for i in json.loads(row["industries_json"])
                                  if i.startswith(profiles.CUSTOM_PREFIX)), "")],
        "_suggested": json.loads(row["suggested_json"]),
    }


def _recipients_table(rows_html: str) -> str:
    return f"""<h2>Team members</h2>
<table>{rows_html or '<tr><td class="muted">No team members yet.</td></tr>'}</table>"""


def _client_row(row: sqlite3.Row, conn: sqlite3.Connection) -> str:
    review = " · review pending" if row["suggested_json"] != "[]" and row["reviewed_at"] is None else ""
    return (
        f'<tr><td><a href="/p/clients/{row["id"]}">{_e(row["client_name"])}</a></td>'
        f'<td>{_e(row["status"])}{review}</td>'
        f'<td>{len(store.active_recipient_rows(conn, row["id"]))} team member(s)</td></tr>'
    )
