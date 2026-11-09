"""The weekly personalised run: build each client's feed, combine one email per team member, check recipients with
the security layer, and deliver. Test runs go only to TEST_RECIPIENTS and record nothing.

Rules agreed for v1:
  - a client with no matching news this week is skipped (no email for that client);
  - a team member on several clients gets ONE combined email;
  - a client is sent only once the manager has reviewed its suggested values;
  - the run is recorded per date, so a Monday run cannot repeat.
"""

from __future__ import annotations

from .settings import PERSONAL_NAME

import logging
import sqlite3
import time
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from .. import db, enrich, google_news
from .. import images as story_images
from ..config import Config
from ..mail_profiles import PRODUCTION, TEST, MailProfile, load_profile
from ..mailer import MailError, RecipientRejected, build_message
from ..security.content import gate_for
from ..security.dns import DnsResolver
from ..security.recipients import check_recipients
from . import analysis, articles, mail, matching, store
from . import profiles
from .profiles import custom_keywords, industry_terms
from .settings import PersonalSettings, cached_settings, personal_sending_enabled, public_base_url

log = logging.getLogger(__name__)


@dataclass
class Feed:
    client: sqlite3.Row
    window_start: datetime
    window_end: datetime
    matches: list[matching.Match]
    items: dict[int, dict] = field(default_factory=dict)

    @property
    def client_id(self) -> int:
        return self.client["id"]


@dataclass
class Report:
    feeds: list[Feed] = field(default_factory=list)
    addresses: int = 0
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (address, reason), e.g. failed security check
    stopped_because: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped and self.stopped_because is None


# Building feeds -------------------------------------------------------------------------------


MIN_DAYS_BETWEEN_FEEDS = 6  # a client gets at most one feed a week, even if its feed day was just changed


def due_clients(conn: sqlite3.Connection, test: bool, now: datetime, tz) -> list[sqlite3.Row]:
    """Clients whose feed is due now: today is their feed day and they have not had one in the last week.
    Test runs preview every reviewed, active client, whatever its day."""
    if test:
        return [
            row for row in store.clients_of(conn)
            if row["status"] == "active" and (row["suggested_json"] == "[]" or row["reviewed_at"] is not None)
        ]
    today = now.astimezone(tz).weekday()
    due = []
    for row in store.active_clients(conn):
        if row["send_day"] != today:
            continue
        last = store.last_issue_at(conn, row["id"])
        if last is not None and now - last < timedelta(days=MIN_DAYS_BETWEEN_FEEDS):
            log.info("Client %s already had its feed this week; skipped", row["client_name"])
            continue
        due.append(row)
    return due


def build_feeds(
    cfg: Config, conn: sqlite3.Connection, settings: PersonalSettings, now: datetime, *, test: bool
) -> list[Feed]:
    clients = due_clients(conn, test, now, cfg.newsletter.timezone)
    if not clients:
        return []
    pool = screened_news(cfg, conn, now - timedelta(days=settings.window_days))
    feeds = [build_one(conn, settings, pool, client, now, dedupe=not test) for client in clients]
    for feed in feeds:
        log.info("Client %s: %d matching stories since %s", feed.client["client_name"], len(feed.matches), feed.window_start.date())
    return feeds


def tiered_keywords(client_keywords, industries, settings) -> tuple[tuple, tuple, tuple]:
    """(core, operational, broad) keywords for one client. Core wins over operational, and operational over broad."""
    operational_list = {term.lower() for term in settings.operational_terms}
    broad_list = {term.lower() for term in settings.broad_terms}
    core: dict[str, str] = {}
    operational: dict[str, str] = {}
    broad: dict[str, str] = {}

    def add(bucket: dict[str, str], value: str) -> None:
        bucket.setdefault(value.lower(), value)

    for keyword in client_keywords:
        key = keyword.lower()
        if key in operational_list:
            add(operational, keyword)
        elif key in broad_list:
            add(broad, keyword)
        else:
            add(core, keyword)
    for keyword in settings.core_terms:
        add(core, keyword)
    for keyword in settings.operational_terms:
        add(operational, keyword)
    for keyword in [*settings.broad_terms, *industry_terms(industries, settings)]:
        add(broad, keyword)
    for key in list(operational):
        if key in core:
            del operational[key]
    for key in list(broad):
        if key in core or key in operational:
            del broad[key]
    return tuple(core.values()), tuple(operational.values()), tuple(broad.values())


def client_terms(client: sqlite3.Row, settings: PersonalSettings) -> matching.Terms:
    """The matching terms of one client: names, competitors and the three keyword tiers."""
    core, operational, broad = tiered_keywords(
        tuple(_json(client["keywords_json"])) + custom_keywords(client_industries(client)),
        client_industries(client), settings,
    )
    return matching.Terms(
        names=(client["client_name"], *_json(client["aliases_json"])),
        competitors=tuple(entry["name"] for entry in _json(client["competitors_json"])),
        core=core,
        operational=operational,
        broad=broad,
        excludes=tuple(_json(client["excludes_json"])),
    )


def resolve_candidate_links(conn: sqlite3.Connection, items: list[dict]) -> None:
    """Google News redirect links among the articles to read are replaced with the publisher's address first."""
    links = [_Link(item["id"], item["url"]) for item in items if google_news.is_google_news_link(item["url"])]
    if not links:
        return
    resolved = google_news.resolve_links(links)
    if not resolved:
        return
    db.update_items(conn, {item_id: {"url": url} for item_id, url in resolved.items()})
    for item in items:
        if item["id"] in resolved:
            item["url"] = resolved[item["id"]]


def analyse_feeds(cfg: Config, conn: sqlite3.Connection, settings: PersonalSettings, feeds: list[Feed],
                  pool: list[dict], now: datetime, *, fetch: bool) -> tuple[dict, dict]:
    """Key takeaways and suggested actions for each matched story, and mentions and competitor watch for each client.
    `fetch` reads articles not yet cached; without it only cached article text is used (previews)."""
    clients = {feed.client_id: (feed.client, client_terms(feed.client, settings), feed.client["website"] or "")
               for feed in feeds}
    if not clients:
        return {}, {}
    wanted = analysis.candidates(pool, clients)
    if fetch:
        resolve_candidate_links(conn, wanted)
        read = articles.ensure(conn, wanted, now=now, user_agent=cfg.fetch.user_agent)
    else:
        read = articles.cached(conn, [item["id"] for item in wanted], now)
    findings = analysis.scan(clients, wanted, read)
    notes: dict = {}
    for feed in feeds:
        terms = clients[feed.client_id][1]
        per_item = {}
        for match in feed.matches:
            item = feed.items[match.item_id]
            article = read.get(match.item_id)
            source_text = article.body if article and article.body else (item["excerpt"] or "")
            per_item[match.item_id] = (
                analysis.takeaways(source_text, terms) or analysis.takeaways(item["excerpt"] or "", terms),
                analysis.suggested_actions(item["title"], source_text, terms,
                                           content_type=match.content_type, strategic=match.strategic),
            )
        notes[feed.client_id] = per_item
    return notes, findings


def build_one(conn: sqlite3.Connection, settings: PersonalSettings, pool: list[dict], client: sqlite3.Row,
              now: datetime, *, dedupe: bool) -> Feed:
    """One client's feed from the screened news pool. `dedupe` skips stories this client already received."""
    earliest = now - timedelta(days=settings.window_days)
    activated = db.from_iso(client["activated_at"]) if client["activated_at"] else earliest
    window_start = max(earliest, activated)
    terms = client_terms(client, settings)
    candidates = [item for item in pool if db.from_iso(item["published_at"]) >= window_start]
    featured = store.already_featured_item_ids(conn, client["id"]) if dedupe else set()
    matches = matching.rank(
        candidates, terms, min_score=settings.min_score, max_stories=settings.max_stories,
        max_per_source=settings.max_per_source, exclude_ids=featured,
    )
    by_id = {item["id"]: item for item in candidates}
    return Feed(client, window_start, now, matches, {match.item_id: by_id[match.item_id] for match in matches})


def screened_news(cfg: Config, conn: sqlite3.Connection, since: datetime) -> list[dict]:
    """Stored news since the given moment that passes the content screen (same rules as the general brief)."""
    rows = conn.execute("SELECT * FROM items WHERE published_at >= ? ORDER BY published_at DESC", (db.to_iso(since),)).fetchall()
    screening = gate_for(cfg).screen_items(rows, cfg.source_map, db.approved_item_ids(conn))
    return screening.allowed


def client_industries(client: sqlite3.Row) -> list[str]:
    return _json(client["industries_json"])


def _json(value: str) -> list:
    import json

    return json.loads(value or "[]")


# Recipients -----------------------------------------------------------------------------------


def plan_recipients(
    conn: sqlite3.Connection, cfg: Config, feeds: list[Feed], *, test_addresses: tuple[str, ...] | None,
    resolver: DnsResolver | None,
) -> tuple[dict[str, list[Feed]], list[tuple[str, str]]]:
    """Team member -> the feeds they get (only non-empty feeds). Production addresses pass the security checks."""
    wanted: dict[str, list[Feed]] = defaultdict(list)
    for feed in feeds:
        if not feed.matches:
            continue
        if test_addresses is not None:
            for address in test_addresses:
                wanted[address].append(feed)
        else:
            for row in store.active_recipient_rows(conn, feed.client_id):
                wanted[row["address"]].append(feed)
    if not wanted:
        return {}, []
    if test_addresses is not None:
        return {address: sorted(group, key=lambda f: f.client["client_name"]) for address, group in wanted.items()}, []

    check = check_recipients(
        list(wanted), cfg.security.recipients, suppressed=db.suppressed_addresses(conn), resolver=resolver
    )
    accepted = {address: sorted(wanted[address], key=lambda f: f.client["client_name"]) for address in check.accepted}
    rejected = [(entry, reason) for entry, reason in check.rejected]
    for finding in check.findings:
        if finding.is_error:
            log.error("Security [recipients]: %s", finding.message)
    return accepted, rejected


# Messages -------------------------------------------------------------------------------------


def message_for(cfg: Config, settings: PersonalSettings, feeds: list[Feed], *, now: datetime,
                unsub_links: dict[int, str] | None, images: dict | None = None,
                notes: dict | None = None, findings: dict | None = None) -> tuple[str, str, str]:
    """(subject, html, text) for one team member's combined email. `images` come from embed_feed_images."""
    sections = []
    for feed in feeds:
        stories = []
        for match in feed.matches:
            item = feed.items[match.item_id]
            picture = (images or {}).get(feed.client_id, {}).get(match.item_id)
            takeaways, actions = (notes or {}).get(feed.client_id, {}).get(match.item_id, ((), ()))
            stories.append(
                mail.StoryLine(
                    title=item["title"],
                    url=item["url"],
                    source=_source_label(cfg, item),
                    published=db.from_iso(item["published_at"]),
                    excerpt=item["excerpt"] or "",
                    reasons=match.reasons,
                    badge=match.content_type,
                    strategic=match.strategic,
                    takeaways=tuple(takeaways),
                    actions=tuple(actions),
                    image_cid=picture[0].cid if picture else None,
                    image_width=picture[1] if picture else 0,
                    image_height=picture[0].display_height(picture[1]) if picture else 0,
                )
            )
        sections.append(
            mail.ClientSection(
                client_name=feed.client["client_name"],
                industries=tuple(_industry_title(settings, industry) for industry in client_industries(feed.client)),
                stories=tuple(stories),
                unsubscribe_url=(unsub_links or {}).get(feed.client_id),
                mentions=tuple((findings or {}).get(feed.client_id, analysis.ClientFindings()).mentions),
                competitor_watch=tuple((findings or {}).get(feed.client_id, analysis.ClientFindings()).competitor_watch),
            )
        )
    html_body, text_body = mail.weekly_email(cfg, now, sections)
    return mail.weekly_subject(cfg, now), html_body, text_body


def _source_label(cfg: Config, item: dict) -> str:
    if item.get("publisher"):
        return item["publisher"]
    source = cfg.source_map.get(item["source_id"])
    return source.name if source else item["source_id"]


def _industry_title(settings: PersonalSettings, industry_id: str) -> str:
    return profiles.industry_title(industry_id, settings)


# Run ------------------------------------------------------------------------------------------


def run_weekly(cfg: Config, conn: sqlite3.Connection, *, now: datetime | None = None, test: bool = False,
               resolver: DnsResolver | None = None, transport=None) -> Report:
    """Build and deliver the weekly personalised feeds.

    Production needs PERSONAL_SEND_ENABLED=true; without it the run builds nothing and records nothing.
    A test run sends one combined copy per clients' feed group to TEST_RECIPIENTS only.
    """
    settings = cached_settings(cfg.root)
    now = now or datetime.now(timezone.utc)
    report = Report()
    profile = load_profile(TEST if test else PRODUCTION, cfg)  # fail fast on bad mail settings

    if not test and not personal_sending_enabled():
        log.warning("Personal feeds are off (PERSONAL_SEND_ENABLED). Nothing was built or sent.")
        return report

    base = None if test else public_base_url()
    if not test and base is None:
        raise MailError("PERSONAL_BASE_URL is not set, so unsubscribe links cannot be written; nothing was built")

    run_date = now.astimezone(cfg.newsletter.timezone).date().isoformat()
    run_id: int | None = None
    if not test:
        run_id = store.start_run(conn, run_date, now)
        if run_id is None:
            log.error("The personal feed already ran for %s; it is not repeated.", run_date)
            return report

    report.feeds = build_feeds(cfg, conn, settings, now, test=test)
    resolve_feed_links(conn, report.feeds)
    pictures = embed_feed_images(cfg, conn, report.feeds, run_key="test" if test else now.date().isoformat())
    pool = screened_news(cfg, conn, now - timedelta(days=settings.window_days))
    notes, findings = analyse_feeds(cfg, conn, settings, report.feeds, pool, now, fetch=True)
    feeds_with_news = [feed for feed in report.feeds if feed.matches]
    if not report.feeds:
        log.info("No client has its feed day today")
    log.info("Personal feeds: %d client(s), %d with news this week", len(report.feeds), len(feeds_with_news))

    own_resolver = resolver is None
    resolver = resolver or DnsResolver()
    try:
        test_addresses = profile.recipients if test else None
        groups, rejected = plan_recipients(conn, cfg, report.feeds, test_addresses=test_addresses, resolver=resolver)
    finally:
        if own_resolver:
            resolver.close()
    report.skipped.extend(rejected)
    report.addresses = len(groups)
    if not groups:
        log.warning("No team member has news this week; nothing is sent.")
        if run_id is not None:
            _record(conn, run_id, report, now, feeds_with_news)
            store.finish_run(conn, run_id, "done", now)
        return report

    if run_id is not None:
        _record(conn, run_id, report, now, feeds_with_news)

    gate = gate_for(cfg)
    messages: list[tuple[str, object]] = []
    for address, feeds in groups.items():
        unsub = None if test else {
            feed.client_id: f"{base}/u/{store.unsubscribe_token(conn, feed.client_id, address)}" for feed in feeds
        }
        subject, html_body, text_body = message_for(cfg, settings, feeds, now=now, unsub_links=unsub, images=pictures,
                                                    notes=notes, findings=findings)
        findings = gate.scan_email(subject, html_body, text_body)
        if any(finding.is_error for finding in findings):
            report.skipped.append((address, "the security layer blocked this email; see the log"))
            log.error("Security: the personal email to %s was not sent: %s", address,
                      "; ".join(f.message for f in findings if f.is_error))
            continue
        inline = {**mail.logo_inline(cfg), **inline_for(feeds, pictures)}
        messages.append((address, (profile.subject(subject), html_body, text_body, inline)))

    if transport is None:
        transport = profile.transport()
    delay = cfg.security.sending.delay_seconds
    try:
        with transport:
            for index, (address, (subject, html_body, text_body, inline)) in enumerate(messages):
                if index and delay:
                    time.sleep(delay)
                if run_id is not None:
                    store.mark_delivery(conn, run_id, address, "sending", now)
                try:
                    transport.send(_message(profile, cfg, subject, html_body, text_body, address, inline))
                except RecipientRejected as exc:
                    _failed(conn, run_id, report, address, exc, now)
                    continue
                except MailError as exc:
                    _failed(conn, run_id, report, address, exc, now)
                    report.stopped_because = str(exc)[:500]
                    log.error("Stopping the personal send: the mail service refused the message itself: %s", exc)
                    break
                report.sent.append(address)
                if run_id is not None:
                    store.mark_delivery(conn, run_id, address, "sent", now)
                log.info("Personal feed sent to %s", address)
    except MailError as exc:
        report.stopped_because = str(exc)[:500]
        log.error("Personal send stopped: %s", exc)

    if run_id is not None:
        store.finish_run(conn, run_id, "done" if report.ok else "failed", now)
    log.info("Personal feed: %d sent, %d failed, %d skipped", len(report.sent), len(report.failed), len(report.skipped))
    return report


def _failed(conn: sqlite3.Connection, run_id: int | None, report: Report, address: str, exc: Exception, now: datetime) -> None:
    error = str(exc)[:500]
    report.failed.append((address, error))
    if run_id is not None:
        store.mark_delivery(conn, run_id, address, "failed", now, error)
    log.error("Personal feed to %s failed: %s", address, error)


class _Link:
    """What google_news.resolve_links needs from a story."""

    def __init__(self, item_id: int, url: str) -> None:
        self.item_id, self.url, self.also_reported_by = item_id, url, ()


def resolve_feed_links(conn: sqlite3.Connection, feeds: list[Feed]) -> None:
    """Replaces Google News redirect links in the matched stories with the publisher's address, as the general brief
    does. Resolved addresses are stored, so the next run does not look them up again."""
    links = [_Link(match.item_id, feed.items[match.item_id]["url"]) for feed in feeds for match in feed.matches]
    resolved = google_news.resolve_links(links)
    if not resolved:
        return
    db.update_items(conn, {item_id: {"url": url} for item_id, url in resolved.items()})
    for feed in feeds:
        for item_id, url in resolved.items():
            if item_id in feed.items:
                feed.items[item_id] = {**feed.items[item_id], "url": url}


class _StoryRef:
    """What the picture and summary helpers need from a story."""

    def __init__(self, item_id: int, url: str, image_url: str | None, excerpt: str) -> None:
        self.item_id, self.url, self.image_url, self.excerpt = item_id, url, image_url, excerpt


def embed_feed_images(cfg: Config, conn: sqlite3.Connection, feeds: list[Feed], *, run_key: str) -> dict:
    """Fills in missing pictures and summaries from the article pages (as the general brief does), stores them, then
    downloads and converts each client's pictures. Returns {client_id: {item_id: (EmbeddedImage, display_width)}}.
    The first strategic story of each client gets the large lead picture; the others get thumbnails."""
    refs: dict[int, _StoryRef] = {}
    for feed in feeds:
        for match in feed.matches:
            item = feed.items[match.item_id]
            refs.setdefault(match.item_id, _StoryRef(match.item_id, item["url"], item.get("image_url"), item["excerpt"] or ""))
    if refs:
        updates = enrich.fill_missing_details(list(refs.values()), cfg.fetch.user_agent)
        if updates:
            db.update_items(conn, updates)
            for feed in feeds:
                for item_id, fields in updates.items():
                    if item_id in feed.items:
                        feed.items[item_id] = {**feed.items[item_id], **fields}

    pictures: dict = {}
    base = cfg.data_dir / "personal" / "images" / run_key
    for feed in feeds:
        if not feed.matches:
            continue
        lead = next((match.item_id for match in feed.matches if match.strategic), None)
        stories = [_StoryRef(match.item_id, "", feed.items[match.item_id].get("image_url"), "") for match in feed.matches]
        embedded = story_images.embed_story_images(stories, lead, base / f"client-{feed.client_id}", cfg.fetch.user_agent)
        per_client = {}
        for item_id, image in embedded.items():
            width = story_images.LEAD_WIDTH if item_id == lead else story_images.THUMB_WIDTH
            # A distinct Content-ID per client: the same story can be a lead for one client and a thumbnail for another.
            per_client[item_id] = (replace(image, cid=f"story-{item_id}-c{feed.client_id}"), width)
        pictures[feed.client_id] = per_client
    return pictures


def inline_for(feeds: list[Feed], pictures: dict | None) -> dict[str, Path]:
    """The picture files to attach to one team member's email, keyed by Content-ID."""
    inline: dict[str, Path] = {}
    for feed in feeds:
        for image, _width in (pictures or {}).get(feed.client_id, {}).values():
            inline[image.cid] = image.path
    return inline


def _record(conn: sqlite3.Connection, run_id: int, report: Report, now: datetime, feeds: list[Feed]) -> None:
    for feed in feeds:
        store.record_issue(conn, run_id, feed.client_id, feed.window_start, feed.window_end, feed.matches, now)


def _message(profile: MailProfile, cfg: Config, subject: str, html_body: str, text_body: str, recipient: str,
             inline: dict | None = None):
    sending = cfg.security.sending
    return build_message(
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        sender=profile.sender,
        sender_name=PERSONAL_NAME,
        recipient=recipient,
        inline_images=inline if inline is not None else mail.logo_inline(cfg),
        reply_to=profile.reply_to,
        unsubscribe_mailbox=(sending.unsubscribe_mailbox or profile.reply_to or profile.sender) if sending.list_unsubscribe else None,
    )


def is_due(now: datetime, settings: PersonalSettings, conn: sqlite3.Connection, tz) -> bool:
    """The daily run is due from its time each day, within 3 hours (a restart still runs it), once per date.
    Each client is sent only on its own feed day, so a day with no clients sends nothing."""
    local = now.astimezone(tz)
    slot = datetime.combine(local.date(), settings.run_time, tzinfo=tz)
    if not (slot <= local <= slot + timedelta(hours=3)):
        return False
    return store.run_for_date(conn, local.date().isoformat()) is None
