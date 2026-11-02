"""`python -m rankuno_brief personal ...` commands for the personalised client feed."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from datetime import datetime, timezone

from ..config import Config
from . import feed, store
from .settings import cached_settings, personal_sending_enabled, public_base_url

log = logging.getLogger(__name__)


def add_parser(commands) -> None:
    personal = commands.add_parser("personal", help="personalised client feeds: admins, the weekly run, status")
    sub = personal.add_subparsers(dest="personal_command", required=True)
    admin = sub.add_parser("add-admin", help="make someone an admin (they then sign in from their own email link)")
    admin.add_argument("email")
    admin.add_argument("name", nargs="?", default="")
    run = sub.add_parser("run", help="build and send this week's client feeds now")
    run.add_argument("--test", action="store_true", help="send a [TEST] copy to TEST_RECIPIENTS only; records nothing")
    sub.add_parser("status", help="show whether the weekly feed is on and what the last run did")
    sub.add_parser("admin-links", help="email each admin a fresh sign-in link (prints them when sending is off)")


def cmd_personal(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    conn.executescript(store.SCHEMA)
    store.seed_default_admins(conn)
    now = datetime.now(timezone.utc)
    if args.personal_command == "admin-links":
        from . import notify
        from .settings import public_base_url

        base = public_base_url()
        if base is None:
            log.error("Set PERSONAL_BASE_URL first: sign-in links are built from it")
            return 1
        settings = cached_settings(cfg.root)
        if not personal_sending_enabled():
            print("Email sending is off (PERSONAL_SEND_ENABLED). Sign-in links, shown once:")
        for admin in notify.admins(conn):
            link = notify.sign_in_link(conn, settings, base, admin["id"], now)
            if personal_sending_enabled():
                from . import mail

                subject, html_body, text_body = mail.notice(
                    cfg, subject=f"{cfg.newsletter.name} · Client Feeds: your sign-in link",
                    heading="Sign in to manage client feeds",
                    paragraphs=["Use the button to sign in. The link works once and expires in "
                                f"{settings.invite_days} days."],
                    button=("Sign in", link),
                )
                notify.send(cfg, admin["email"], subject, html_body, text_body)
                print(f"Emailed {admin['email']}")
            else:
                print(f"{admin['email']}: {link}")
        return 0
    if args.personal_command == "add-admin":
        from ..security.dns import DnsResolver
        from ..security.recipients import check_recipients, normalize_address

        address = normalize_address(args.email)
        if address is None:
            log.error("Not a valid email address: %s", args.email)
            return 1
        with DnsResolver() as resolver:
            check = check_recipients([address], cfg.security.recipients, resolver=resolver)
        if not check.accepted:
            log.error("%s is not allowed: %s", address, check.rejected[0][1] if check.rejected else "rejected")
            return 1
        person = store.add_person(conn, address, args.name, "admin", now)
        base = public_base_url()
        if base is None:
            log.error("Set PERSONAL_BASE_URL first: the sign-in link is built from it")
            return 1
        raw = store.create_invite(conn, person, "cli", now, cached_settings(cfg.root).invite_days)
        print(f"Admin: {address}. Sign-in link (works once; open it to sign in, then manage from /a/):")
        print(f"{base}/m/{raw}")
        return 0
    if args.personal_command == "run":
        report = feed.run_weekly(cfg, conn, now=now, test=args.test)
        print(f"Personal feed: {len(report.sent)} sent, {len(report.failed)} failed, {len(report.skipped)} skipped")
        return 0 if report.ok else 1
    if args.personal_command == "status":
        settings = cached_settings(cfg.root)
        print(f"Weekly feed: {'ON' if personal_sending_enabled() else 'OFF'} (PERSONAL_SEND_ENABLED)")
        print(f"Sign-in links point at: {public_base_url() or 'NOT SET (PERSONAL_BASE_URL)'}")
        print(f"Runs daily at {settings.run_time:%H:%M}; each client's feed goes out on its own feed day")
        for row in store.clients_of(conn):
            print(f"  {row['client_name']}: {row['status']}, {len(store.active_recipient_rows(conn, row['id']))} team member(s)")
        return 0
    return 2
