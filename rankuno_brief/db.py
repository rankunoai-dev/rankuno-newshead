"""SQLite storage. All timestamps are ISO 8601 strings in UTC, so they sort correctly as text."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

SENT_STATUSES = ("sent", "partial")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    url                  TEXT NOT NULL,
    etag                 TEXT,
    last_modified        TEXT,
    last_success_at      TEXT,
    last_error           TEXT,
    last_error_at        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY,
    url_hash       TEXT NOT NULL UNIQUE,
    source_id      TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,   -- the original article
    discovered_via TEXT,            -- where we found it, when different (e.g. a Reddit thread)
    publisher      TEXT,            -- original publisher's name, when an aggregator reports it
    author         TEXT,
    published_at   TEXT NOT NULL,
    fetched_at     TEXT NOT NULL,
    excerpt        TEXT NOT NULL DEFAULT '',
    image_url      TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_published_at ON items (published_at);

CREATE TABLE IF NOT EXISTS fetch_runs (
    id              INTEGER PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    sources_ok      INTEGER,
    sources_failed  INTEGER,
    items_new       INTEGER
);

CREATE TABLE IF NOT EXISTS fetch_log (
    run_id       INTEGER NOT NULL REFERENCES fetch_runs (id),
    source_id    TEXT NOT NULL,
    status       TEXT NOT NULL,     -- ok | not_modified | error
    http_status  INTEGER,
    items_seen   INTEGER NOT NULL,
    items_new    INTEGER NOT NULL,
    error        TEXT,
    duration_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    id            INTEGER PRIMARY KEY,
    issue_date    TEXT NOT NULL UNIQUE,  -- local send date, e.g. 2026-09-14
    number        INTEGER NOT NULL,
    subject       TEXT NOT NULL,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    status        TEXT NOT NULL,         -- built | sent | partial
    html_path     TEXT NOT NULL,
    text_path     TEXT NOT NULL,
    built_at      TEXT NOT NULL,
    sent_at       TEXT,
    html_sha256   TEXT,                  -- fingerprints of the screened files; a send refuses if they changed
    text_sha256   TEXT
);

CREATE TABLE IF NOT EXISTS issue_items (
    issue_id  INTEGER NOT NULL REFERENCES issues (id) ON DELETE CASCADE,
    item_id   INTEGER NOT NULL REFERENCES items (id),
    section   TEXT NOT NULL,
    position  INTEGER NOT NULL,
    PRIMARY KEY (issue_id, item_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    issue_id      INTEGER NOT NULL REFERENCES issues (id),
    recipient     TEXT NOT NULL,
    status        TEXT NOT NULL,  -- sending | sent | failed
    attempted_at  TEXT NOT NULL,
    sent_at       TEXT,
    error         TEXT,
    PRIMARY KEY (issue_id, recipient)
);

-- Security screen results for flagged items (items with nothing found have no row).
CREATE TABLE IF NOT EXISTS moderation (
    item_id      INTEGER PRIMARY KEY REFERENCES items (id),
    decision     TEXT NOT NULL,   -- blocked | held
    reasons      TEXT NOT NULL,
    checked_at   TEXT NOT NULL,
    approved_at  TEXT,            -- an editor approved a held item; ignored for blocked items
    approved_by  TEXT
);

CREATE TABLE IF NOT EXISTS recipient_health (
    address          TEXT PRIMARY KEY,
    hard_failures    INTEGER NOT NULL DEFAULT 0,  -- consecutive permanent (5xx) rejections
    last_error       TEXT,
    last_failure_at  TEXT,
    suppressed_at    TEXT                          -- set once hard_failures reaches the limit
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release: (table, column, definition). Applied once to older databases.
_ADDED_COLUMNS = (
    ("items", "publisher", "TEXT"),
    ("issues", "html_sha256", "TEXT"),
    ("issues", "text_sha256", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            with conn:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


# Sources and fetching -------------------------------------------------------------------------


def sync_sources(conn: sqlite3.Connection, sources: Iterable) -> None:
    with conn:
        conn.executemany(
            "INSERT INTO sources (id, name, url) VALUES (?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET name = excluded.name, url = excluded.url",
            [(source.id, source.name, source.feed_url) for source in sources],
        )


def source_validators(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    rows = conn.execute("SELECT id, etag, last_modified FROM sources")
    return {row["id"]: (row["etag"], row["last_modified"]) for row in rows}


def start_fetch_run(conn: sqlite3.Connection, started_at: datetime) -> int:
    with conn:
        return conn.execute("INSERT INTO fetch_runs (started_at) VALUES (?)", (to_iso(started_at),)).lastrowid


def record_outcome(conn: sqlite3.Connection, run_id: int, outcome, fetched_at: datetime) -> int:
    """Store one source's result atomically: new items, feed validators and health. Returns new item count."""
    now = to_iso(fetched_at)
    new_items = 0
    with conn:
        for item in outcome.items:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO items (url_hash, source_id, title, url, discovered_via, publisher, author, "
                "published_at, fetched_at, excerpt, image_url) VALUES (:url_hash, :source_id, :title, :url, "
                ":discovered_via, :publisher, :author, :published_at, :fetched_at, :excerpt, :image_url)",
                {**item, "fetched_at": now},
            )
            new_items += cursor.rowcount

        if outcome.status == "error":
            conn.execute(
                "UPDATE sources SET last_error = ?, last_error_at = ?, "
                "consecutive_failures = consecutive_failures + 1 WHERE id = ?",
                (outcome.error, now, outcome.source.id),
            )
        elif outcome.status == "ok":
            conn.execute(
                "UPDATE sources SET etag = ?, last_modified = ?, last_success_at = ?, "
                "consecutive_failures = 0, last_error = NULL WHERE id = ?",
                (outcome.etag, outcome.last_modified, now, outcome.source.id),
            )
        else:  # not modified: the feed is healthy and the stored validators are still correct
            conn.execute(
                "UPDATE sources SET last_success_at = ?, consecutive_failures = 0, last_error = NULL WHERE id = ?",
                (now, outcome.source.id),
            )

        conn.execute(
            "INSERT INTO fetch_log (run_id, source_id, status, http_status, items_seen, items_new, error, "
            "duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                outcome.source.id,
                outcome.status,
                outcome.http_status,
                outcome.entries_seen,
                new_items,
                outcome.error,
                outcome.duration_ms,
            ),
        )
    return new_items


def finish_fetch_run(conn: sqlite3.Connection, run_id: int, finished_at: datetime, ok: int, failed: int, new: int) -> None:
    with conn:
        conn.execute(
            "UPDATE fetch_runs SET finished_at = ?, sources_ok = ?, sources_failed = ?, items_new = ? WHERE id = ?",
            (to_iso(finished_at), ok, failed, new, run_id),
        )


def source_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.id, s.name, s.last_success_at, s.consecutive_failures, s.last_error, "
        "(SELECT COUNT(*) FROM items i WHERE i.source_id = s.id) AS item_count "
        "FROM sources s ORDER BY s.consecutive_failures DESC, s.id"
    ).fetchall()


_UPDATABLE_ITEM_FIELDS = frozenset({"url", "image_url", "excerpt"})


def update_items(conn: sqlite3.Connection, updates: Mapping[int, Mapping[str, str]]) -> None:
    """Store details found while building an issue, e.g. {item_id: {"url": ..., "image_url": ...}}."""
    with conn:
        for item_id, fields in updates.items():
            unknown = set(fields) - _UPDATABLE_ITEM_FIELDS
            if unknown:
                raise ValueError(f"Cannot update item fields: {', '.join(sorted(unknown))}")
            if fields:
                assignments = ", ".join(f"{name} = ?" for name in fields)
                conn.execute(f"UPDATE items SET {assignments} WHERE id = ?", (*fields.values(), item_id))


# Issues ---------------------------------------------------------------------------------------


def candidate_items(conn: sqlite3.Connection, published_since: datetime) -> list[sqlite3.Row]:
    """Items published since the given moment that have not appeared in an issue already sent."""
    placeholders = ", ".join("?" for _ in SENT_STATUSES)
    return conn.execute(
        f"""
        SELECT * FROM items
        WHERE published_at >= ?
          AND id NOT IN (
              SELECT ii.item_id FROM issue_items ii
              JOIN issues s ON s.id = ii.issue_id
              WHERE s.status IN ({placeholders})
          )
        ORDER BY published_at DESC
        """,
        (to_iso(published_since), *SENT_STATUSES),
    ).fetchall()


def last_sent_issue(conn: sqlite3.Connection) -> sqlite3.Row | None:
    placeholders = ", ".join("?" for _ in SENT_STATUSES)
    return conn.execute(
        f"SELECT * FROM issues WHERE status IN ({placeholders}) ORDER BY window_end DESC LIMIT 1", SENT_STATUSES
    ).fetchone()


def sent_issue_dates(conn: sqlite3.Connection) -> set[str]:
    placeholders = ", ".join("?" for _ in SENT_STATUSES)
    rows = conn.execute(f"SELECT issue_date FROM issues WHERE status IN ({placeholders})", SENT_STATUSES)
    return {row["issue_date"] for row in rows}


def sent_issue_count(conn: sqlite3.Connection) -> int:
    placeholders = ", ".join("?" for _ in SENT_STATUSES)
    return conn.execute(f"SELECT COUNT(*) FROM issues WHERE status IN ({placeholders})", SENT_STATUSES).fetchone()[0]


def get_issue(conn: sqlite3.Connection, issue_date: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM issues WHERE issue_date = ?", (issue_date,)).fetchone()


def latest_unsent_issue(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM issues WHERE status = 'built' ORDER BY issue_date DESC LIMIT 1").fetchone()


def save_issue(
    conn: sqlite3.Connection,
    *,
    issue_date: str,
    number: int,
    subject: str,
    window_start: datetime,
    window_end: datetime,
    html_path: str,
    text_path: str,
    built_at: datetime,
    stories: Iterable[tuple[int, str]],
    html_sha256: str | None = None,
    text_sha256: str | None = None,
) -> int:
    """Create or rebuild an unsent issue. `stories` is (item_id, section) in reading order."""
    with conn:
        existing = conn.execute("SELECT id, status FROM issues WHERE issue_date = ?", (issue_date,)).fetchone()
        if existing and existing["status"] in SENT_STATUSES:
            raise ValueError(f"Issue {issue_date} has already been sent and cannot be rebuilt")
        values = (
            number, subject, to_iso(window_start), to_iso(window_end), html_path, text_path, to_iso(built_at),
            html_sha256, text_sha256,
        )
        if existing:
            issue_id = existing["id"]
            conn.execute(
                "UPDATE issues SET number = ?, subject = ?, window_start = ?, window_end = ?, html_path = ?, "
                "text_path = ?, built_at = ?, html_sha256 = ?, text_sha256 = ?, status = 'built' WHERE id = ?",
                (*values, issue_id),
            )
            conn.execute("DELETE FROM issue_items WHERE issue_id = ?", (issue_id,))
        else:
            issue_id = conn.execute(
                "INSERT INTO issues (number, subject, window_start, window_end, html_path, text_path, built_at, "
                "html_sha256, text_sha256, issue_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'built')",
                (*values, issue_date),
            ).lastrowid
        conn.executemany(
            "INSERT INTO issue_items (issue_id, item_id, section, position) VALUES (?, ?, ?, ?)",
            [(issue_id, item_id, section, position) for position, (item_id, section) in enumerate(stories)],
        )
    return issue_id


def set_issue_status(conn: sqlite3.Connection, issue_id: int, status: str, sent_at: datetime | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE issues SET status = ?, sent_at = ? WHERE id = ?",
            (status, to_iso(sent_at) if sent_at else None, issue_id),
        )


# Deliveries -----------------------------------------------------------------------------------


def delivery_status(conn: sqlite3.Connection, issue_id: int, recipient: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM deliveries WHERE issue_id = ? AND recipient = ?", (issue_id, recipient)
    ).fetchone()
    return row["status"] if row else None


def mark_delivery(
    conn: sqlite3.Connection, issue_id: int, recipient: str, status: str, at: datetime, error: str | None = None
) -> None:
    moment = to_iso(at)
    with conn:
        conn.execute(
            "INSERT INTO deliveries (issue_id, recipient, status, attempted_at, sent_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (issue_id, recipient) DO UPDATE SET status = excluded.status, "
            "attempted_at = excluded.attempted_at, sent_at = excluded.sent_at, error = excluded.error",
            (issue_id, recipient, status, moment, moment if status == "sent" else None, error),
        )


# Moderation -----------------------------------------------------------------------------------


def record_moderation(conn: sqlite3.Connection, screened_ids: Iterable[int], verdicts: Iterable, at: datetime) -> None:
    """Store the latest screen result. Approvals survive re-screening; rows for items now clean are removed."""
    moment = to_iso(at)
    flagged = {verdict.item_id: verdict for verdict in verdicts}
    with conn:
        conn.executemany(
            "INSERT INTO moderation (item_id, decision, reasons, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (item_id) DO UPDATE SET decision = excluded.decision, reasons = excluded.reasons, "
            "checked_at = excluded.checked_at",
            [
                (item_id, "blocked" if verdict.decision == "blocked" else "held", verdict.reasons, moment)
                for item_id, verdict in flagged.items()
            ],
        )
        conn.executemany(
            "DELETE FROM moderation WHERE item_id = ? AND approved_at IS NULL",
            [(item_id,) for item_id in screened_ids if item_id not in flagged],
        )


def approved_item_ids(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT item_id FROM moderation WHERE approved_at IS NOT NULL")
    return {row["item_id"] for row in rows}


def moderation_entries(conn: sqlite3.Connection, published_since: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.item_id, m.decision, m.reasons, m.approved_at, m.approved_by, i.title, i.excerpt, i.url, "
        "i.source_id, i.published_at FROM moderation m JOIN items i ON i.id = m.item_id "
        "WHERE i.published_at >= ? ORDER BY m.decision, i.published_at DESC",
        (to_iso(published_since),),
    ).fetchall()


def approve_item(conn: sqlite3.Connection, item_id: int, by: str, at: datetime) -> str:
    """Approve a held item. Returns 'approved', 'blocked' (not allowed) or 'unknown'."""
    with conn:
        row = conn.execute("SELECT decision FROM moderation WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return "unknown"
        if row["decision"] == "blocked":
            return "blocked"
        conn.execute(
            "UPDATE moderation SET approved_at = ?, approved_by = ? WHERE item_id = ?", (to_iso(at), by, item_id)
        )
    return "approved"


def revoke_approval(conn: sqlite3.Connection, item_id: int) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE moderation SET approved_at = NULL, approved_by = NULL WHERE item_id = ? AND approved_at IS NOT NULL",
            (item_id,),
        )
    return cursor.rowcount > 0


# Recipient health -----------------------------------------------------------------------------


def record_hard_failure(conn: sqlite3.Connection, address: str, error: str, at: datetime, limit: int) -> bool:
    """Count a permanent rejection. Returns True when this failure suppresses the address."""
    moment = to_iso(at)
    with conn:
        conn.execute(
            "INSERT INTO recipient_health (address, hard_failures, last_error, last_failure_at) VALUES (?, 1, ?, ?) "
            "ON CONFLICT (address) DO UPDATE SET hard_failures = hard_failures + 1, "
            "last_error = excluded.last_error, last_failure_at = excluded.last_failure_at",
            (address.lower(), error, moment),
        )
        cursor = conn.execute(
            "UPDATE recipient_health SET suppressed_at = ? "
            "WHERE address = ? AND suppressed_at IS NULL AND hard_failures >= ?",
            (moment, address.lower(), limit),
        )
    return cursor.rowcount > 0


def clear_hard_failures(conn: sqlite3.Connection, address: str) -> None:
    with conn:
        conn.execute(
            "UPDATE recipient_health SET hard_failures = 0 WHERE address = ? AND suppressed_at IS NULL",
            (address.lower(),),
        )


def suppressed_addresses(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT address FROM recipient_health WHERE suppressed_at IS NOT NULL")
    return {row["address"] for row in rows}


def unsuppress(conn: sqlite3.Connection, address: str) -> bool:
    with conn:
        cursor = conn.execute(
            "UPDATE recipient_health SET suppressed_at = NULL, hard_failures = 0 WHERE address = ? "
            "AND suppressed_at IS NOT NULL",
            (address.strip().lower(),),
        )
    return cursor.rowcount > 0
