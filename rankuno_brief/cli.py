"""Command line: python -m rankuno_brief {fetch,build,send,run,serve,sources,security}."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import compose, db, enrich, google_news, images, render, slots
from .config import Config, ConfigError, load_config
from .fetch import run_fetch
from .mail_profiles import PRODUCTION, TEST, MailProfile, load_profile
from .mailer import MailError, RecipientRejected, build_message, deliver_issue, mime_bytes
from .scoring import Scorer
from .security import preflight
from .security.content import Verdict, gate_for
from .security.dns import DnsResolver
from .security.findings import ERROR, WARNING, has_errors
from .security.recipients import normalize_address

log = logging.getLogger("rankuno_brief")

CHECK_LABELS = {
    "integrity": "Files unchanged",
    "content": "Content",
    "recipients": "Recipients",
    "spam-signals": "Spam signals",
    "sender-auth": "Sender (SPF/DKIM/DMARC)",
}


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

    send = commands.add_parser("send", help="email a built issue (production profile, or --test)")
    send.add_argument("--date", type=date.fromisoformat, help="issue date to send; default: latest unsent issue")
    send.add_argument(
        "--test",
        action="store_true",
        help="send a [TEST] copy with the TEST_ mail settings to TEST_RECIPIENTS only; nothing is marked sent",
    )

    run = commands.add_parser("run", help="fetch, build and send right now, whatever the schedule")
    run.add_argument("--test", action="store_true", help="send a [TEST] copy to TEST_RECIPIENTS only")
    run.add_argument("--no-fetch", action="store_true", help="build from the news already stored")

    commands.add_parser("serve", help="hosting mode: run the schedule and the admin page (PORT, ADMIN_TOKEN)")
    commands.add_parser("sources", help="show the health of every source")

    security = commands.add_parser("security", help="content screening, recipient and deliverability checks")
    security_commands = security.add_subparsers(dest="security_command", required=True)
    check = security_commands.add_parser("check", help="run every pre-send check without sending anything")
    check.add_argument("--date", type=date.fromisoformat, help="issue date; default: latest unsent issue")
    check.add_argument("--test", action="store_true", help="check the TEST_ mail settings and TEST_RECIPIENTS")
    review = security_commands.add_parser("review", help="list stories the content screen held or blocked")
    review.add_argument("--days", type=int, default=14, help="how far back to look (default 14)")
    review.add_argument("--all", action="store_true", help="also list stories that would not qualify anyway")
    approve = security_commands.add_parser("approve", help="let held stories into the next build")
    approve.add_argument("item_ids", nargs="+", type=int, metavar="ID")
    revoke = security_commands.add_parser("revoke", help="withdraw an approval")
    revoke.add_argument("item_ids", nargs="+", type=int, metavar="ID")
    unsuppress = security_commands.add_parser("unsuppress", help="let a suppressed address receive the brief again")
    unsuppress.add_argument("addresses", nargs="+", metavar="EMAIL")
    scan = security_commands.add_parser("scan", help="test a headline, phrase or link against the content filter")
    scan.add_argument("text")

    args = parser.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    setup_logging(cfg)

    if args.command == "serve":
        from .server import serve

        return serve(cfg)

    conn = db.connect(cfg.db_path)
    try:
        handler = {
            "fetch": cmd_fetch,
            "build": cmd_build,
            "send": cmd_send,
            "run": cmd_run,
            "sources": cmd_sources,
            "security": cmd_security,
        }[args.command]
        return handler(cfg, conn, args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except MailError as exc:
        log.error("%s", exc)
        return 1
    finally:
        conn.close()


def cmd_fetch(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    summary = run_fetch(cfg, conn)
    log.info("Fetch finished: %d sources ok, %d failed, %d new items", summary.ok, summary.failed, summary.new_items)
    return 1 if summary.ok == 0 else 0


def cmd_build(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    return 0 if build_issue(cfg, conn, issue_date=args.date, offline=args.offline) else 1


def cmd_send(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    profile = load_profile(TEST if args.test else PRODUCTION, cfg)
    return send_issue(cfg, conn, profile, issue_date=args.date)


def cmd_run(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    return run_pipeline(cfg, conn, test=args.test, fetch=not args.no_fetch)


def run_pipeline(cfg: Config, conn: sqlite3.Connection, *, test: bool, fetch: bool = True, send: bool = True) -> int:
    """Fetch, build and send now. Production issues are only sent when PROD_SEND_ENABLED is true."""
    profile = load_profile(TEST if test else PRODUCTION, cfg) if send else None  # fail before a long fetch
    if fetch:
        cmd_fetch(cfg, conn, argparse.Namespace())  # if every source fails, the stored news is still built
    issue_date = build_issue(cfg, conn)
    if issue_date is None or profile is None:
        return 0 if issue_date else 1
    if not profile.send_enabled:
        log.warning("Issue %s is built. Production sending is off (PROD_SEND_ENABLED), so nothing was sent.", issue_date)
        return 0
    return send_issue(cfg, conn, profile, issue_date=issue_date)


def build_issue(cfg: Config, conn: sqlite3.Connection, *, issue_date: date | None = None, offline: bool = False) -> date | None:
    """Build (or rebuild) an unsent issue. Returns its date, or None when nothing was built."""
    now = datetime.now(timezone.utc)
    tz = cfg.newsletter.timezone
    sent_dates = {date.fromisoformat(value) for value in db.sent_issue_dates(conn)}
    issue_date = issue_date or slots.resolve_issue_date(now.astimezone(tz), cfg.newsletter.send_slots, sent_dates)

    existing = db.get_issue(conn, issue_date.isoformat())
    if existing and existing["status"] in db.SENT_STATUSES:
        log.error("Issue %s has already been sent; it will not be rebuilt", issue_date)
        return None

    last_sent = db.last_sent_issue(conn)
    if last_sent:
        window_start = db.from_iso(last_sent["window_end"])
    else:
        window_start = now - timedelta(days=cfg.issue.first_issue_lookback_days)
    rows = db.candidate_items(conn, window_start - timedelta(hours=cfg.issue.grace_hours))

    # Security screen: offensive, sensitive or unsafe items never reach selection.
    gate = gate_for(cfg)
    screening = gate.screen_items(rows, cfg.source_map, db.approved_item_ids(conn))
    db.record_moderation(conn, [row["id"] for row in rows], screening.verdicts, now)
    _log_screen("Security screen", screening.verdicts)

    content = compose.build_content(screening.allowed, cfg, now)
    if not content.story_count:
        log.error("No stories qualified for issue %s. Run 'fetch' first or check the source health.", issue_date)
        return None

    if not offline:
        resolved = google_news.resolve_links(content.stories)
        db.update_items(conn, {item_id: {"url": url} for item_id, url in resolved.items()})
        removed = compose.drop_duplicate_urls(content)
        if removed:
            log.info("Removed %d stories that duplicated another story's article", removed)
        db.update_items(conn, enrich.fill_missing_details(content.stories, cfg.fetch.user_agent))

    # Links, images and summaries can change above, so the final stories are screened again.
    final_verdicts = gate.screen_content(content, cfg.source_map, db.approved_item_ids(conn))
    db.record_moderation(conn, (), final_verdicts, now)
    _log_screen("Final security screen", final_verdicts)
    if not content.story_count:
        log.error("No stories left for issue %s after the security screen.", issue_date)
        return None

    out_dir = cfg.data_dir / "issues" / issue_date.isoformat()
    # Pictures are embedded, not linked: Outlook cannot show WebP and asks before loading linked images.
    lead_item = content.top_stories[0].item_id if content.top_stories else None
    story_images = {} if offline else images.embed_story_images(content.stories, lead_item, out_dir / "images", cfg.fetch.user_agent)

    meta = render.IssueMeta(
        number=db.sent_issue_count(conn) + 1,
        issue_date=issue_date,
        window_start=window_start,
        window_end=now,
        subject=render.make_subject(cfg, issue_date),
    )
    html_body, text_body = render.render_issue(content, meta, cfg, story_images=story_images)

    # Output gate: the finished email is checked before anything is written.
    findings = gate.scan_email(meta.subject, html_body, text_body)
    if has_errors(findings):
        for finding in findings:
            log.error("Security: %s", finding.message)
        log.error("Build of issue %s stopped by the security layer; no files were written.", issue_date)
        return None
    preview_body, _ = render.render_issue(content, meta, cfg, preview=True, story_images=story_images)

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path, text_path, preview_path = out_dir / "email.html", out_dir / "email.txt", out_dir / "preview.html"
    html_path.write_text(html_body, encoding="utf-8")
    text_path.write_text(text_body, encoding="utf-8")
    preview_path.write_text(preview_body, encoding="utf-8")
    # A ready-made message file: double-click it to see exactly how Outlook renders the issue.
    try:
        profile = load_profile(PRODUCTION, cfg)
    except ConfigError:
        profile = None
    embedded = {image.cid: image.path for image in story_images.values()}
    eml = _message(cfg, profile, meta.subject, html_body, text_body, _preview_recipient(cfg),
                   {**render.inline_images(cfg), **embedded})
    (out_dir / "email.eml").write_bytes(mime_bytes(eml))

    db.save_issue(
        conn,
        issue_date=issue_date.isoformat(),
        number=meta.number,
        subject=meta.subject,
        window_start=window_start,
        window_end=now,
        html_path=html_path.relative_to(cfg.data_dir).as_posix(),
        text_path=text_path.relative_to(cfg.data_dir).as_posix(),
        built_at=now,
        stories=[(story.item_id, story.section_id) for story in content.stories],
        html_sha256=preflight.html_fingerprint(html_body, {cid: path.read_bytes() for cid, path in embedded.items()}),
        text_sha256=preflight.sha256(text_body),
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
    return issue_date


def send_issue(cfg: Config, conn: sqlite3.Connection, profile: MailProfile, *, issue_date: date | None = None) -> int:
    """Send with the given mail profile. Test copies go only to TEST_RECIPIENTS and mark nothing as sent."""
    issue = db.get_issue(conn, issue_date.isoformat()) if issue_date else db.latest_unsent_issue(conn)
    if issue is None:
        log.error("No built issue found to send. Run 'build' first.")
        return 1
    if not profile.is_test:
        if issue["status"] == "sent":
            log.error("Issue %s was already sent to everyone", issue["issue_date"])
            return 1
        if not profile.send_enabled:
            log.error(
                "Production sending is off, so issue %s was not sent. Set PROD_SEND_ENABLED=true to send it; "
                "test copies (--test) are not affected.",
                issue["issue_date"],
            )
            return 1

    loaded = _read_issue_files(cfg, issue)
    if loaded is None:
        return 1
    html_body, text_body, inline = loaded

    def make_message(recipient: str):
        return _message(cfg, profile, profile.subject(issue["subject"]), html_body, text_body, recipient, inline)

    with DnsResolver() as resolver:
        report = preflight.run_preflight(
            cfg,
            conn,
            issue,
            html_body=html_body,
            text_body=text_body,
            requested_recipients=profile.recipients,
            make_message=make_message,
            sender=profile.sender,
            smtp_host=profile.server_host,
            resolver=resolver,
            images=_story_image_bytes(cfg, inline),
        )
    for finding in report.findings:
        (log.error if finding.is_error else log.warning)("Security [%s]: %s", finding.check, finding.message)
    if report.blocked:
        errors = sum(finding.is_error for finding in report.findings)
        log.error("Nothing was sent: the security layer found %d problem(s). Full report: security check", errors)
        return 1
    recipients = report.recipients.accepted
    skipped = len(report.recipients.rejected)
    delay = cfg.security.sending.delay_seconds
    log.info(
        "Sending issue %s with the %s profile (%s, from %s) to %d recipient(s) from %s",
        issue["issue_date"],
        profile.name,
        profile.provider,
        profile.sender,
        len(recipients),
        profile.recipients_source,
    )

    if profile.is_test:
        failed = 0
        with profile.transport() as transport:
            for index, recipient in enumerate(recipients):
                if index and delay:
                    time.sleep(delay)
                try:
                    transport.send(make_message(recipient))
                except RecipientRejected as exc:
                    failed += 1
                    log.error("Test copy to %s was refused: %s", recipient, exc)
                    continue
                log.info("Sent test copy of issue %s to %s", issue["issue_date"], recipient)
        return 1 if skipped or failed else 0

    with profile.transport() as transport:
        delivery = deliver_issue(
            conn,
            issue,
            recipients,
            transport,
            make_message,
            pause_seconds=delay,
            suppress_after=cfg.security.recipients.suppress_after_hard_failures,
        )
    log.info(
        "Issue %s: %d sent, %d already had it, %d failed, %d uncertain, %d not attempted, %d skipped by security checks",
        issue["issue_date"],
        len(delivery.sent),
        len(delivery.already_sent),
        len(delivery.failed),
        len(delivery.uncertain),
        len(delivery.not_attempted),
        skipped,
    )
    if delivery.uncertain:
        log.warning(
            "Uncertain deliveries (an earlier send stopped mid-way; check before resending): %s",
            ", ".join(delivery.uncertain),
        )
    if delivery.not_attempted:
        log.warning(
            "The send stopped (%s). Not attempted: %s. Run 'send' again later; nobody receives it twice.",
            delivery.stopped_because,
            ", ".join(delivery.not_attempted),
        )
    return 0 if delivery.complete and not skipped else 1


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


# Security commands ------------------------------------------------------------------------------


def cmd_security(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # headlines can contain characters the console cannot show
    handler = {
        "check": _security_check,
        "review": _security_review,
        "approve": _security_approve,
        "revoke": _security_revoke,
        "unsuppress": _security_unsuppress,
        "scan": _security_scan,
    }[args.security_command]
    return handler(cfg, conn, args)


def _security_check(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    profile = load_profile(TEST if args.test else PRODUCTION, cfg)
    issue = db.get_issue(conn, args.date.isoformat()) if args.date else db.latest_unsent_issue(conn)
    if args.date and issue is None:
        print(f"No issue has been built for {args.date}.")
        return 1
    html_body = text_body = ""
    inline = render.inline_images(cfg)
    if issue is not None:
        loaded = _read_issue_files(cfg, issue)
        if loaded is None:
            return 1
        html_body, text_body, inline = loaded
    subject = profile.subject(issue["subject"] if issue is not None else render.make_subject(cfg, date.today()))

    with DnsResolver() as resolver:
        report = preflight.run_preflight(
            cfg,
            conn,
            issue,
            html_body=html_body,
            text_body=text_body,
            requested_recipients=profile.recipients,
            make_message=lambda recipient: _message(cfg, profile, subject, html_body, text_body, recipient, inline),
            sender=profile.sender,
            smtp_host=profile.server_host,
            resolver=resolver,
            images=_story_image_bytes(cfg, inline),
        )

    print()
    if issue is not None:
        built = issue["built_at"][:16].replace("T", " ")
        print(f"SECURITY CHECK  issue {issue['issue_date']} (No. {issue['number']}, built {built} UTC)")
    else:
        print("SECURITY CHECK  no unsent issue is built yet, so only the recipients and the sender are checked")
    print("\nMAIL PROFILE")
    for line in profile.describe():
        print(f"  {line}")
    print()
    for check in report.checks:
        found = report.for_check(check)
        errors = sum(finding.is_error for finding in found)
        warnings = len(found) - errors
        parts = [f"{errors} error(s)" if errors else "", f"{warnings} warning(s)" if warnings else ""]
        print(f"  {CHECK_LABELS[check]:<26} {', '.join(part for part in parts if part) or 'ok'}")

    for level, heading in ((ERROR, "ERRORS (these stop the send)"), (WARNING, "WARNINGS")):
        found = [finding for finding in report.findings if finding.level == level]
        if found:
            print(f"\n{heading}")
            for finding in found:
                print(f"  - [{finding.check}] {finding.message}")

    accepted = report.recipients.accepted
    print(f"\nRECIPIENTS  {len(accepted)} will receive it, {len(report.recipients.rejected)} skipped  ({profile.recipients_source})")
    for address in accepted[:50]:
        print(f"  {address}")
    if len(accepted) > 50:
        print(f"  ... and {len(accepted) - 50} more")

    sender_report = report.sender
    if sender_report is not None:
        print(f"\nSENDER  {sender_report.address} via {sender_report.smtp_host} ({sender_report.provider or 'unrecognised provider'})")
        print(f"  SPF:   {sender_report.spf[0] if sender_report.spf else 'none found'}")
        print(f"  DMARC: {sender_report.dmarc[0] if sender_report.dmarc else 'none found'}")
        if sender_report.provider_signed:
            print(f"  DKIM:  signed by {sender_report.provider}")
        else:
            print(f"  DKIM:  {', '.join(sender_report.dkim_selectors) or 'none found'}")

    now, scorer = datetime.now(timezone.utc), Scorer(cfg.taxonomy)
    held = [
        row
        for row in db.moderation_entries(conn, now - timedelta(days=14))
        if _review_state(row) == "held" and _would_qualify(cfg, scorer, row, now)
    ]
    if held:
        print(f"\nCONTENT SCREEN  {len(held)} stor{'y' if len(held) == 1 else 'ies'} from the last 14 days held for review: security review")

    if report.blocked:
        result = "BLOCKED: fix the errors above; nothing would be sent."
    elif not profile.send_enabled:
        result = "Checks pass, but production sending is off (PROD_SEND_ENABLED)."
    else:
        result = "Ready to send."
    print(f"\nRESULT  {result}")
    return 1 if report.blocked else 0


def _security_review(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    rows = db.moderation_entries(conn, now - timedelta(days=args.days))
    if not args.all:
        scorer = Scorer(cfg.taxonomy)
        rows = [row for row in rows if _would_qualify(cfg, scorer, row, now)]
    groups = (
        ("held", "HELD: left out until approved  (approve with: security approve ID)"),
        ("approved", "APPROVED: can appear in the next build  (undo with: security revoke ID)"),
        ("blocked", "BLOCKED: never published  (for a false positive, add the phrase to allow_phrases in config/content_filter.yaml)"),
    )
    for state, heading in groups:
        entries = [row for row in rows if _review_state(row) == state]
        if not entries:
            continue
        print(f"\n{heading}")
        for row in entries:
            print(f"  {row['item_id']:>6}  {row['published_at'][:10]}  {row['source_id'][:26]:<26}  {row['title'][:90]}")
            print(f"  {'':>6}  {row['reasons'][:160]}")
    if not rows:
        suffix = "" if args.all else " among stories that would otherwise qualify (--all shows every flagged item)"
        print(f"Nothing held or blocked in the last {args.days} days{suffix}.")
    return 0


def _security_approve(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    reviewer = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    status = 0
    for item_id in args.item_ids:
        outcome = db.approve_item(conn, item_id, reviewer, datetime.now(timezone.utc))
        if outcome == "approved":
            print(f"{item_id}: approved. It can appear from the next 'build'.")
        elif outcome == "blocked":
            print(
                f"{item_id}: blocked stories cannot be approved. If it is a false positive, add the phrase to "
                "allow_phrases in config/content_filter.yaml and build again."
            )
            status = 1
        else:
            print(f"{item_id}: not found among screened stories (see: security review)")
            status = 1
    return status


def _security_revoke(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    status = 0
    for item_id in args.item_ids:
        if db.revoke_approval(conn, item_id):
            print(f"{item_id}: approval withdrawn. It is left out from the next 'build'.")
        else:
            print(f"{item_id}: no approval to withdraw")
            status = 1
    return status


def _security_unsuppress(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    status = 0
    for address in args.addresses:
        if db.unsuppress(conn, address):
            print(f"{address}: will receive the brief again")
        else:
            print(f"{address}: was not suppressed")
            status = 1
    return status


def _security_scan(cfg: Config, conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    gate = gate_for(cfg)
    value = args.text.strip()
    is_link = bool(re.match(r"https?://", value, re.IGNORECASE))
    flags = gate.check_link(value) if is_link else gate.check_text(value, "text")
    if not is_link and gate.clean_title(value) != value:
        print(f"Shown in the email as: {gate.clean_title(value)}")
    if not flags:
        print("ALLOWED: nothing found")
        return 0
    print("BLOCKED" if any(flag.action == "block" for flag in flags) else "HELD for review")
    for flag in flags:
        print(f'  - {flag.category} ({flag.action}): "{flag.term}"')
    return 1


# Helpers ----------------------------------------------------------------------------------------


def _message(cfg: Config, profile: MailProfile | None, subject: str, html_body: str, text_body: str,
             recipient: str, images=None):
    sender = profile.sender if profile else "brief@rankuno.com"
    reply_to = profile.reply_to if profile else (cfg.security.sending.reply_to or None)
    sending = cfg.security.sending
    return build_message(
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        sender=sender,
        sender_name=profile.sender_name if profile else cfg.newsletter.name,
        recipient=recipient,
        inline_images=images if images is not None else render.inline_images(cfg),
        reply_to=reply_to,
        unsubscribe_mailbox=(sending.unsubscribe_mailbox or reply_to or sender) if sending.list_unsubscribe else None,
    )


def _log_screen(label: str, verdicts: list[Verdict]) -> None:
    counts = {decision: sum(verdict.decision == decision for verdict in verdicts) for decision in ("blocked", "held", "approved")}
    if any(counts.values()):
        log.info(
            "%s: %d blocked, %d held for review, %d approved by an editor (details: security review)",
            label,
            counts["blocked"],
            counts["held"],
            counts["approved"],
        )


def _review_state(row: sqlite3.Row) -> str:
    return "approved" if row["decision"] == "held" and row["approved_at"] else row["decision"]


def _would_qualify(cfg: Config, scorer: Scorer, row: sqlite3.Row, now: datetime) -> bool:
    """Whether a flagged story is relevant enough that it would have been considered for the brief."""
    source = cfg.source_map.get(row["source_id"])
    if source is None or not source.enabled:
        return False
    age_days = (now - db.from_iso(row["published_at"])).total_seconds() / 86400
    result = scorer.score(row["title"], row["excerpt"], source, age_days)
    return result is not None and result.score >= cfg.issue.min_score


def issue_file(cfg: Config, stored: str) -> Path:
    """Issue files are stored relative to the data directory; issues built before DATA_DIR existed, to the project."""
    path = Path(stored)
    if path.is_absolute():
        return path
    in_data_dir = cfg.data_dir / path
    return in_data_dir if in_data_dir.exists() or not (cfg.root / path).exists() else cfg.root / path


_CID_SOURCE = re.compile(r'src="cid:([A-Za-z0-9_.-]+)"')


def issue_images(cfg: Config, issue: sqlite3.Row, html_body: str) -> dict[str, Path]:
    """Every image the email embeds, by Content-ID: the logos and the story pictures saved at build time."""
    logos = render.inline_images(cfg)
    folder = issue_file(cfg, issue["html_path"]).parent / "images"
    return {cid: logos.get(cid) or folder / f"{cid}.jpg" for cid in dict.fromkeys(_CID_SOURCE.findall(html_body))}


def _story_image_bytes(cfg: Config, inline: dict[str, Path]) -> dict[str, bytes]:
    logos = render.inline_images(cfg)
    return {cid: path.read_bytes() for cid, path in inline.items() if cid not in logos}


def _read_issue_files(cfg: Config, issue: sqlite3.Row) -> tuple[str, str, dict[str, Path]] | None:
    """(html, text, embedded images) of a built issue, or None if a file is missing."""
    try:
        html_body = issue_file(cfg, issue["html_path"]).read_text(encoding="utf-8")
        text_body = issue_file(cfg, issue["text_path"]).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        log.error("The email files for issue %s are missing (%s). Run 'build' again.", issue["issue_date"], exc.filename)
        return None
    inline = issue_images(cfg, issue, html_body)
    missing = [str(path) for path in inline.values() if not path.is_file()]
    if missing:
        log.error("Images embedded in issue %s are missing (%s). Run 'build' again.", issue["issue_date"], ", ".join(missing))
        return None
    return html_body, text_body, inline


def _preview_recipient(cfg: Config) -> str:
    valid = (address for entry in cfg.delivery.recipients if (address := normalize_address(entry)))
    return next(valid, "preview@rankuno.com")


def setup_logging(cfg: Config) -> None:
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
