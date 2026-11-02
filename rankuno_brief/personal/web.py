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
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
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

ROUTE_PREFIXES = ("/m/", "/p/", "/a/", "/u/")
ROUTE_EXACT = ("/p", "/a", "/logout")
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
        if path.startswith("/m/"):
            return self._sign_in(handler, method, path[3:], conn, now)
        if path.startswith("/u/"):
            return self._unsubscribe(handler, method, path[3:], conn, now)
        if path == "/logout" and method == "POST":
            return self._logout(handler, form, conn, now)

        person = store.session_for(conn, _cookie(handler), now)
        if person is None:
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
        body = f"""<h2>{'New client' if is_new else _e(_one(fields, 'client_name') or 'Client')}</h2>
{notice_html}{error_html}{approval_note}{suggested_note}
<form method="post" action="{action_path}" class="stack">
<input type="hidden" name="csrf" value="{_e(person['csrf'])}">
<label>Client name <span class="req">*</span><input name="client_name" value="{_e(_one(fields, 'client_name'))}" maxlength="120" required></label>
<label>Website<input name="website" value="{_e(_one(fields, 'website'))}" placeholder="https://example.com"></label>
<label>Other names (one per line)<textarea name="aliases" rows="2">{_e(_one(fields, 'aliases'))}</textarea></label>
<fieldset><legend>Operating industries / lines of business <span class="req">*</span></legend>{ind_boxes}</fieldset>
<label>Competitors (one per line: Name | website)<textarea name="competitors" rows="5">{_e(_one(fields, 'competitors'))}</textarea></label>
<label>Keywords to follow (one per line)<textarea name="keywords" rows="4">{_e(_one(fields, 'keywords'))}</textarea></label>
<label>Words to exclude (one per line)<textarea name="excludes" rows="2">{_e(_one(fields, 'excludes'))}</textarea></label>
<label>Team members who receive the weekly feed (one per line or separated by commas)
<textarea name="recipients" rows="4" placeholder="name@rankuno.com">{_e(_one(fields, 'recipients'))}</textarea></label>
<label class="check"><input type="checkbox" name="activate" value="1"> Activate: send this client's feed every Monday</label>
<div class="actions"><button name="action" value="save">Save</button></div>
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
        managers = store.managers(conn)
        client_rows = []
        for c in store.clients_of(conn):
            owner = store.person(conn, c["manager_id"])
            options = "".join(
                f'<option value="{m["id"]}"{" selected" if m["id"] == c["manager_id"] else ""}>{_e(m["email"])}</option>'
                for m in managers
            )
            count = len(store.active_recipient_rows(conn, c["id"]))
            pending = "review pending" if c["suggested_json"] != "[]" and c["reviewed_at"] is None else ""
            client_rows.append(
                f"""<tr><td><a href="/p/clients/{c['id']}">{_e(c['client_name'])}</a></td><td>{_e(owner['email'] if owner else '')}</td>
<td>{_e(c['status'])} {pending}</td><td>{count}</td>
<td><form method="post" action="/a/clients/{c['id']}/manager" class="inline"><input type="hidden" name="csrf" value="{csrf}">
<select name="manager_id">{options}</select><button class="secondary">Reassign</button></form>
<form method="post" action="/a/clients/{c['id']}/status" class="inline"><input type="hidden" name="csrf" value="{csrf}">
<input type="hidden" name="status" value="{'paused' if c['status'] == 'active' else 'active'}">
<button class="secondary">{'Pause' if c['status'] == 'active' else 'Resume'}</button></form></td></tr>"""
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
            nav = (f'<p class="muted">Signed in as {_e(person["email"])} · <a href="/p/">My clients</a>'
                   + (' · <a href="/a/">Admin</a>' if person["role"] == "admin" else "")
                   + f'<form method="post" action="/logout" class="inline"><input type="hidden" name="csrf" value="{csrf}">'
                   '<button class="secondary">Sign out</button></form></p>')
        document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · Client feeds</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; color: #262626; background: #F2F2F2; margin: 0; padding: 24px 16px; }}
  main {{ max-width: 960px; margin: 0 auto; background: #fff; border-top: 5px solid #DF212A; padding: 24px 28px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 16px; margin: 22px 0 8px; }}
  p, td, label, legend {{ font-size: 14px; line-height: 21px; }} .muted {{ color: #707070; }}
  .error {{ color: #A1161D; font-weight: bold; }} .ok {{ color: #1B6E2E; font-weight: bold; }}
  .notice {{ background: #FFF4F4; border-left: 4px solid #DF212A; padding: 10px 12px; margin: 12px 0; }}
  .stack label {{ display: block; margin: 12px 0; }} .stack input, .stack textarea, .stack select {{ display: block; width: 100%; box-sizing: border-box; padding: 8px; margin-top: 4px; font-size: 14px; border: 1px solid #BDBDBD; }}
  .check {{ display: block !important; font-weight: normal; }} .check input {{ width: auto !important; display: inline !important; }}
  fieldset {{ border: 1px solid #E6E6E6; margin: 14px 0; padding: 10px 12px; }}
  button {{ background: #DF212A; color: #fff; border: 0; padding: 10px 16px; font-size: 14px; font-weight: bold; cursor: pointer; }}
  button.secondary {{ background: #262626; padding: 6px 10px; font-size: 13px; }}
  .button {{ background: #DF212A; color: #fff; padding: 10px 16px; text-decoration: none; font-weight: bold; }}
  form.inline {{ display: inline-block; margin: 0 6px 6px 0; }} .req {{ color: #DF212A; }}
  table {{ border-collapse: collapse; width: 100%; }} td {{ border-bottom: 1px solid #E6E6E6; padding: 6px 8px 6px 0; vertical-align: top; }}
  pre {{ background: #F7F7F7; padding: 10px; font-size: 12px; white-space: pre-wrap; word-break: break-all; }}
  a {{ color: #DF212A; }}
</style></head><body><main>
<h1>The RankUno Brief · Client feeds</h1>
{nav}
{body}
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
