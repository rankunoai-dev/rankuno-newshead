"""Command line: python -m rankuno_brief {fetch,build,send,sources}."""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import compose, db, enrich, google_news, render, slots
from .config import Config, ConfigError, load_config
from .fetch import run_fetch
from .mailer import SmtpSettings, SmtpTransport, build_message, deliver_issue

log = logging.getLogger("rankuno_brief")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rankuno_brief", description="The RankUno Brief news digest")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("fetch", help="download all feeds and store new items (run daily)")

    build = commands.add_parser("build", help="assemble the next issue and write its HTML and text files")
    build.add_argument("--date", type=date.fromisoformat, help="issue date (YYYY-MM-DD); default: next send slot")
    build.add_argument(
        "--offline",
        action="store_true",
        help="skip web lookups (Google News link resolution, missing images and summaries)",
    )

    send = commands.add_parser("send", help="email a built issue to the configured recipients")
    send.add_argument("--date", type=date.fromisoformat, help="issue date to send; default: latest unsent issue")
    send.add_argument(
        "--test",
        nargs="*",
        metavar="EMAIL",
        help="send a [TEST] copy (to these addresses, or the configured recipients) without marking the issue sent",
    )

    commands.add_parser("sources", help="show the health of every source")

    args = parser.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    _setup_logging(cfg)

    conn = db.connect(cfg.db_path)
    try:
        handler = {"fetch": cmd_fetch, "build": cmd_build, "send": cmd_send, "sources": cmd_sources}[args.command]
        return handler(cfg, conn, args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except smtplib.SMTPAuthenticationError as exc:
        server_message = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else exc.smtp_error
        log.error(
            "The mail server rejected the login (%s: %s). Check SMTP_USERNAME and SMTP_PASSWORD in .env; "
            "for Gmail the password must be an App Password created on that same account.",
            exc.smtp_code,
            " ".join(server_message.split()),
        )
        return 1
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Could not connect to the mail server: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        conn.close()


def cmd_fetch(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    summary = run_fetch(cfg, conn)
    log.info("Fetch finished: %d sources ok, %d failed, %d new items", summary.ok, summary.failed, summary.new_items)
    return 1 if summary.ok == 0 else 0


def cmd_build(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    tz = cfg.newsletter.timezone
    sent_dates = {date.fromisoformat(value) for value in db.sent_issue_dates(conn)}
    issue_date = args.date or slots.resolve_issue_date(now.astimezone(tz), cfg.newsletter.send_slots, sent_dates)

    existing = db.get_issue(conn, issue_date.isoformat())
    if existing and existing["status"] in db.SENT_STATUSES:
        log.error("Issue %s has already been sent; it will not be rebuilt", issue_date)
        return 1

    last_sent = db.last_sent_issue(conn)
    if last_sent:
        window_start = db.from_iso(last_sent["window_end"])
    else:
        window_start = now - timedelta(days=cfg.issue.first_issue_lookback_days)
    rows = db.candidate_items(conn, window_start - timedelta(hours=cfg.issue.grace_hours))
    content = compose.build_content(rows, cfg, now)
    if not content.story_count:
        log.error("No stories qualified for issue %s. Run 'fetch' first or check the source health.", issue_date)
        return 1

    if not args.offline:
        resolved = google_news.resolve_links(content.stories)
        db.update_items(conn, {item_id: {"url": url} for item_id, url in resolved.items()})
        removed = compose.drop_duplicate_urls(content)
        if removed:
            log.info("Removed %d stories that duplicated another story's article", removed)
        db.update_items(conn, enrich.fill_missing_details(content.stories, cfg.fetch.user_agent))

    meta = render.IssueMeta(
        number=db.sent_issue_count(conn) + 1,
        issue_date=issue_date,
        window_start=window_start,
        window_end=now,
        subject=render.make_subject(cfg, issue_date),
    )
    html_body, text_body = render.render_issue(content, meta, cfg)
    preview_body, _ = render.render_issue(content, meta, cfg, preview=True)

    out_dir = cfg.data_dir / "issues" / issue_date.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path, text_path, preview_path = out_dir / "email.html", out_dir / "email.txt", out_dir / "preview.html"
    html_path.write_text(html_body, encoding="utf-8")
    text_path.write_text(text_body, encoding="utf-8")
    preview_path.write_text(preview_body, encoding="utf-8")
    # A ready-made message file: double-click it to see exactly how Outlook renders the issue.
    eml = build_message(
        subject=meta.subject,
        html_body=html_body,
        text_body=text_body,
        sender=os.environ.get("MAIL_FROM") or "brief@rankuno.com",
        sender_name=cfg.newsletter.name,
        recipient=(cfg.delivery.recipients or ("preview@rankuno.com",))[0],
        inline_images=render.inline_images(cfg),
    )
    (out_dir / "email.eml").write_bytes(eml.as_bytes())

    db.save_issue(
        conn,
        issue_date=issue_date.isoformat(),
        number=meta.number,
        subject=meta.subject,
        window_start=window_start,
        window_end=now,
        html_path=str(html_path.relative_to(cfg.root)),
        text_path=str(text_path.relative_to(cfg.root)),
        built_at=now,
        stories=[(story.item_id, story.section_id) for story in content.stories],
    )

    size = len(html_body.encode("utf-8"))
    log.info(
        "Built issue %s (No. %d): %d stories from %d sources, %.0f KB. Open in a browser: %s",
        issue_date,
        meta.number,
        content.story_count,
        content.source_count,
        size / 1000,
        preview_path,
    )
    if size > render.GMAIL_CLIP_BYTES:
        log.warning("HTML is %.0f KB; Gmail clips messages above ~102 KB. Lower issue.max_stories.", size / 1000)
    return 0


def cmd_send(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if args.date:
        issue = db.get_issue(conn, args.date.isoformat())
    else:
        issue = db.latest_unsent_issue(conn)
    if issue is None:
        log.error("No built issue found to send. Run 'build' first.")
        return 1

    html_body = (cfg.root / issue["html_path"]).read_text(encoding="utf-8")
    text_body = (cfg.root / issue["text_path"]).read_text(encoding="utf-8")
    smtp = SmtpSettings.from_env()
    images = render.inline_images(cfg)

    def make_message(recipient: str, subject: str = issue["subject"]):
        return build_message(
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            sender=smtp.sender,
            sender_name=smtp.sender_name,
            recipient=recipient,
            inline_images=images,
        )

    if args.test is not None:
        recipients = args.test or list(cfg.delivery.recipients)
        with SmtpTransport(smtp) as transport:
            for recipient in recipients:
                transport.send(make_message(recipient, subject=f"[TEST] {issue['subject']}"))
                log.info("Sent test copy of issue %s to %s", issue["issue_date"], recipient)
        return 0

    if issue["status"] == "sent":
        log.error("Issue %s was already sent to everyone", issue["issue_date"])
        return 1
    if not cfg.delivery.recipients:
        log.error("No recipients configured in settings.yaml (delivery.recipients)")
        return 1

    with SmtpTransport(smtp) as transport:
        report = deliver_issue(conn, issue, cfg.delivery.recipients, transport, make_message)
    log.info(
        "Issue %s: %d sent, %d already had it, %d failed, %d uncertain",
        issue["issue_date"],
        len(report.sent),
        len(report.already_sent),
        len(report.failed),
        len(report.uncertain),
    )
    if report.uncertain:
        log.warning(
            "Uncertain deliveries (an earlier send stopped mid-way; check before resending): %s",
            ", ".join(report.uncertain),
        )
    return 0 if report.complete else 1


def cmd_sources(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    db.sync_sources(conn, cfg.sources)
    configured = cfg.source_map
    rows = [row for row in db.source_health(conn) if row["id"] in configured]
    print(f"{'SOURCE':28} {'ITEMS':>5}  {'FAILS':>5}  {'LAST SUCCESS (UTC)':20}  LAST ERROR")
    for row in rows:
        print(
            f"{row['id']:28} {row['item_count']:>5}  {row['consecutive_failures']:>5}  "
            f"{(row['last_success_at'] or '-')[:19]:20}  {row['last_error'] or ''}"
        )
    return 0


def _setup_logging(cfg: Config) -> None:
    log_dir: Path = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logfile = RotatingFileHandler(log_dir / "brief.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    logfile.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers[:] = [console, logfile]
    logging.getLogger("httpx").setLevel(logging.WARNING)
