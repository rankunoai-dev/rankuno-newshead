"""Storage for the personalised client feed. Same SQLite file as the newsletter; every table is prefixed personal_.

Secrets are stored as SHA-256 hashes (invitation and session tokens). Unsubscribe tokens are stored as they
appear in emails: they only remove one recipient from one client's feed, so they are not sign-in credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db

ROLES = ("admin", "manager")
CLIENT_STATUSES = ("draft", "active", "paused")

SCHEMA = """
CREATE TABLE IF NOT EXISTS personal_people (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL,            -- admin | manager
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    approved_at TEXT,                     -- managers can activate clients only once approved; admins from the start
    shared      INTEGER NOT NULL DEFAULT 0  -- 1 = the shared admin login, not a person; never emailed
);

CREATE TABLE IF NOT EXISTS personal_settings (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personal_invites (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES personal_people (id),
    token_hash  TEXT NOT NULL UNIQUE,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS personal_sessions (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES personal_people (id),
    token_hash  TEXT NOT NULL UNIQUE,
    csrf        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE TABLE IF NOT EXISTS personal_clients (
    id               INTEGER PRIMARY KEY,
    manager_id       INTEGER NOT NULL REFERENCES personal_people (id),
    client_name      TEXT NOT NULL,
    website          TEXT NOT NULL DEFAULT '',
    aliases_json     TEXT NOT NULL DEFAULT '[]',
    industries_json  TEXT NOT NULL DEFAULT '[]',
    competitors_json TEXT NOT NULL DEFAULT '[]',   -- [{"name": ..., "website": ...}]
    keywords_json    TEXT NOT NULL DEFAULT '[]',
    excludes_json    TEXT NOT NULL DEFAULT '[]',
    suggested_json   TEXT NOT NULL DEFAULT '[]',   -- fields the engine pre-filled, awaiting the manager's review
    reviewed_at      TEXT,                          -- manager confirmed the suggested values
    status           TEXT NOT NULL DEFAULT 'draft', -- draft | active | paused
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    activated_at     TEXT,
    send_day         INTEGER NOT NULL DEFAULT 0    -- weekday of the weekly feed: 0 = Monday ... 6 = Sunday
);

CREATE TABLE IF NOT EXISTS personal_recipients (
    client_id     INTEGER NOT NULL REFERENCES personal_clients (id),
    address       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | unsubscribed
    unsub_token   TEXT NOT NULL UNIQUE,
    added_at      TEXT NOT NULL,
    unsubscribed_at TEXT,
    PRIMARY KEY (client_id, address)
);

CREATE TABLE IF NOT EXISTS personal_runs (
    id           INTEGER PRIMARY KEY,
    run_date     TEXT NOT NULL UNIQUE,   -- local date of the weekly run
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL           -- running | done | failed
);

CREATE TABLE IF NOT EXISTS personal_issues (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES personal_runs (id),
    client_id    INTEGER NOT NULL REFERENCES personal_clients (id),
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    story_count  INTEGER NOT NULL,
    UNIQUE (run_id, client_id)
);

CREATE TABLE IF NOT EXISTS personal_issue_items (
    issue_id  INTEGER NOT NULL REFERENCES personal_issues (id),
    item_id   INTEGER NOT NULL REFERENCES items (id),
    client_id INTEGER NOT NULL,
    score     REAL NOT NULL,
    PRIMARY KEY (issue_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_personal_issue_items_client ON personal_issue_items (client_id, item_id);

CREATE TABLE IF NOT EXISTS personal_deliveries (
    run_id       INTEGER NOT NULL REFERENCES personal_runs (id),
    recipient    TEXT NOT NULL,
    status       TEXT NOT NULL,           -- sending | sent | failed
    attempted_at TEXT NOT NULL,
    sent_at      TEXT,
    error        TEXT,
    PRIMARY KEY (run_id, recipient)
);

-- A manager asks for approval of their account (one open request at a time).
CREATE TABLE IF NOT EXISTS personal_requests (
    id            INTEGER PRIMARY KEY,
    manager_id    INTEGER NOT NULL REFERENCES personal_people (id),
    status        TEXT NOT NULL,        -- pending | approved | rejected
    submitted_at  TEXT NOT NULL,
    decided_at    TEXT,
    decided_by    TEXT,
    reason        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_personal_requests_status ON personal_requests (status);

-- Who changed what, for the admins.
CREATE TABLE IF NOT EXISTS personal_audit (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);
"""


DEFAULT_ADMINS = (
    ("rajat.singh@rankuno.com", "Rajat Singh"),
    ("naeem.gari@rankuno.com", "Naeem Gari"),
    ("prithviraj.pednekar@rankuno.com", "Prithviraj Pednekar"),
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = db.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    seed_default_admins(conn)
    return conn


# Columns added after the first personal-feed release: (table, column, definition).
_ADDED_COLUMNS = (
    ("personal_people", "approved_at", "TEXT"),
    ("personal_people", "shared", "INTEGER NOT NULL DEFAULT 0"),
    ("personal_clients", "send_day", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            with conn:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def seed_default_admins(conn: sqlite3.Connection, now: datetime | None = None) -> None:
    moment = _iso(now or datetime.now(timezone.utc))
    with conn:
        for email, name in DEFAULT_ADMINS:
            conn.execute(
                """INSERT INTO personal_people (email, name, role, active, created_at, approved_at)
                   VALUES (?, ?, 'admin', 1, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET role = 'admin', active = 1,
                       approved_at = COALESCE(personal_people.approved_at, excluded.approved_at)""",
                (email, name, moment, moment),
            )


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _iso(moment: datetime) -> str:
    return db.to_iso(moment)


# People ---------------------------------------------------------------------------------------------


def add_person(conn: sqlite3.Connection, email: str, name: str, role: str, now: datetime) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    with conn:
        conn.execute(
            """INSERT INTO personal_people (email, name, role, active, created_at) VALUES (?, ?, ?, 1, ?)
               ON CONFLICT(email) DO UPDATE SET name = excluded.name, role = excluded.role, active = 1""",
            (email, name, role, _iso(now)),
        )
    return conn.execute("SELECT id FROM personal_people WHERE email = ?", (email,)).fetchone()["id"]


def person(conn: sqlite3.Connection, person_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM personal_people WHERE id = ?", (person_id,)).fetchone()


def person_by_email(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM personal_people WHERE email = ?", (email,)).fetchone()


def people(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM personal_people ORDER BY role, email").fetchall()


def managers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM personal_people WHERE active = 1 AND role = 'manager' ORDER BY email"
    ).fetchall()


# Invitations and sessions ---------------------------------------------------------------------


def create_invite(conn: sqlite3.Connection, person_id: int, created_by: str, now: datetime, days: int) -> str:
    raw = new_token()
    with conn:
        conn.execute(
            "INSERT INTO personal_invites (person_id, token_hash, created_by, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (person_id, hash_token(raw), created_by, _iso(now), _iso(now + timedelta(days=days))),
        )
    return raw


def redeem_invite(conn: sqlite3.Connection, raw: str, now: datetime) -> sqlite3.Row | None:
    """Single use: returns the person and marks the invitation used. Expired, used or revoked links return None."""
    row = conn.execute(
        """SELECT i.id AS invite_id, p.* FROM personal_invites i JOIN personal_people p ON p.id = i.person_id
           WHERE i.token_hash = ? AND i.used_at IS NULL AND i.expires_at > ? AND p.active = 1""",
        (hash_token(raw), _iso(now)),
    ).fetchone()
    if row is None:
        return None
    with conn:
        cursor = conn.execute(
            "UPDATE personal_invites SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (_iso(now), row["invite_id"]),
        )
    return row if cursor.rowcount == 1 else None  # a concurrent click lost the race


def create_session(conn: sqlite3.Connection, person_id: int, now: datetime, days: int) -> tuple[str, str]:
    raw, csrf = new_token(), new_token()
    with conn:
        conn.execute(
            "INSERT INTO personal_sessions (person_id, token_hash, csrf, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (person_id, hash_token(raw), csrf, _iso(now), _iso(now + timedelta(days=days))),
        )
    return raw, csrf


def session_for(conn: sqlite3.Connection, raw: str | None, now: datetime) -> sqlite3.Row | None:
    """The signed-in person and the session's CSRF token, or None."""
    if not raw:
        return None
    return conn.execute(
        """SELECT s.csrf, s.token_hash, p.* FROM personal_sessions s JOIN personal_people p ON p.id = s.person_id
           WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? AND p.active = 1""",
        (hash_token(raw), _iso(now)),
    ).fetchone()


def end_session(conn: sqlite3.Connection, raw: str, now: datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE personal_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (_iso(now), hash_token(raw)),
        )


def deactivate_person(conn: sqlite3.Connection, person_id: int, now: datetime) -> None:
    """Offboarding: the person can no longer sign in; their sessions end now. Reassign their clients separately."""
    with conn:
        conn.execute("UPDATE personal_people SET active = 0 WHERE id = ?", (person_id,))
        conn.execute(
            "UPDATE personal_sessions SET revoked_at = ? WHERE person_id = ? AND revoked_at IS NULL",
            (_iso(now), person_id),
        )


# Clients --------------------------------------------------------------------------------------------


def create_client(conn: sqlite3.Connection, manager_id: int, data: dict, now: datetime) -> int:
    with conn:
        cursor = conn.execute(
            """INSERT INTO personal_clients (manager_id, client_name, website, aliases_json, industries_json,
                   competitors_json, keywords_json, excludes_json, suggested_json, send_day, reviewed_at, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'draft', ?, ?)""",
            (
                manager_id, data["client_name"], data["website"], json.dumps(data["aliases"]),
                json.dumps(data["industries"]), json.dumps(data["competitors"]), json.dumps(data["keywords"]),
                json.dumps(data["excludes"]), json.dumps(data["suggested"]), int(data.get("send_day", 0)), _iso(now), _iso(now),
            ),
        )
    return cursor.lastrowid


def update_client(conn: sqlite3.Connection, client_id: int, data: dict, now: datetime, *, reviewed: bool) -> None:
    """Overwrites the editable fields. A reviewed flag records that the manager confirmed the suggested values."""
    with conn:
        conn.execute(
            """UPDATE personal_clients SET client_name = ?, website = ?, aliases_json = ?, industries_json = ?,
                   competitors_json = ?, keywords_json = ?, excludes_json = ?, suggested_json = ?, send_day = ?,
                   reviewed_at = CASE WHEN ? THEN ? ELSE reviewed_at END, version = version + 1, updated_at = ?
               WHERE id = ?""",
            (
                data["client_name"], data["website"], json.dumps(data["aliases"]), json.dumps(data["industries"]),
                json.dumps(data["competitors"]), json.dumps(data["keywords"]), json.dumps(data["excludes"]),
                json.dumps(data["suggested"]), int(data.get("send_day", 0)), 1 if reviewed else 0, _iso(now), _iso(now), client_id,
            ),
        )


def mark_reviewed(conn: sqlite3.Connection, client_id: int, now: datetime) -> None:
    with conn:
        conn.execute("UPDATE personal_clients SET reviewed_at = ? WHERE id = ?", (_iso(now), client_id))


def set_client_status(conn: sqlite3.Connection, client_id: int, status: str, now: datetime) -> None:
    if status not in CLIENT_STATUSES:
        raise ValueError(f"status must be one of {CLIENT_STATUSES}")
    with conn:
        conn.execute(
            """UPDATE personal_clients SET status = ?, updated_at = ?,
                   activated_at = CASE WHEN ? = 'active' AND activated_at IS NULL THEN ? ELSE activated_at END
               WHERE id = ?""",
            (status, _iso(now), status, _iso(now), client_id),
        )


def reassign_client(conn: sqlite3.Connection, client_id: int, manager_id: int, now: datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE personal_clients SET manager_id = ?, updated_at = ? WHERE id = ?", (manager_id, _iso(now), client_id)
        )


def client(conn: sqlite3.Connection, client_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM personal_clients WHERE id = ?", (client_id,)).fetchone()


def clients_of(conn: sqlite3.Connection, manager_id: int | None = None) -> list[sqlite3.Row]:
    if manager_id is None:
        return conn.execute("SELECT * FROM personal_clients ORDER BY client_name COLLATE NOCASE").fetchall()
    return conn.execute(
        "SELECT * FROM personal_clients WHERE manager_id = ? ORDER BY client_name COLLATE NOCASE", (manager_id,)
    ).fetchall()


def active_clients(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Clients whose feed is due: active, reviewed if they had suggestions, and with at least one active recipient."""
    return conn.execute(
        """SELECT c.* FROM personal_clients c
           WHERE c.status = 'active'
             AND (c.suggested_json = '[]' OR c.reviewed_at IS NOT NULL)
             AND EXISTS (SELECT 1 FROM personal_recipients r WHERE r.client_id = c.id AND r.status = 'active')
           ORDER BY c.id"""
    ).fetchall()


def last_issue_at(conn: sqlite3.Connection, client_id: int) -> datetime | None:
    """When this client's last weekly feed was built, or None."""
    row = conn.execute("SELECT MAX(window_end) AS last FROM personal_issues WHERE client_id = ?", (client_id,)).fetchone()
    return db.from_iso(row["last"]) if row and row["last"] else None


def delete_client(conn: sqlite3.Connection, client_id: int) -> None:
    """Removes a client with its team list and feed history. Used only after the shared admin password is re-entered."""
    with conn:
        conn.execute("DELETE FROM personal_issue_items WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM personal_issues WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM personal_recipients WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM personal_clients WHERE id = ?", (client_id,))


def client_suggested(row: sqlite3.Row) -> list[str]:
    return json.loads(row["suggested_json"])


# Recipients -----------------------------------------------------------------------------------------


def add_recipients(conn: sqlite3.Connection, client_id: int, addresses: list[str], now: datetime) -> None:
    with conn:
        for address in addresses:
            conn.execute(
                """INSERT INTO personal_recipients (client_id, address, status, unsub_token, added_at)
                   VALUES (?, ?, 'active', ?, ?)
                   ON CONFLICT(client_id, address) DO UPDATE SET status = 'active', unsubscribed_at = NULL""",
                (client_id, address, new_token(), _iso(now)),
            )


def remove_recipient(conn: sqlite3.Connection, client_id: int, address: str) -> None:
    with conn:
        conn.execute("DELETE FROM personal_recipients WHERE client_id = ? AND address = ?", (client_id, address))


def recipients_of(conn: sqlite3.Connection, client_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM personal_recipients WHERE client_id = ? ORDER BY address", (client_id,)
    ).fetchall()


def active_recipient_rows(conn: sqlite3.Connection, client_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM personal_recipients WHERE client_id = ? AND status = 'active' ORDER BY address", (client_id,)
    ).fetchall()


def unsubscribe(conn: sqlite3.Connection, token: str, now: datetime) -> sqlite3.Row | None:
    """Stops one recipient getting one client's feed. Returns the client and address, or None for an unknown link."""
    row = conn.execute(
        """SELECT r.address, c.client_name, r.client_id FROM personal_recipients r
           JOIN personal_clients c ON c.id = r.client_id WHERE r.unsub_token = ?""",
        (token,),
    ).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute(
            "UPDATE personal_recipients SET status = 'unsubscribed', unsubscribed_at = ? WHERE unsub_token = ?",
            (_iso(now), token),
        )
    return row


def unsubscribe_token(conn: sqlite3.Connection, client_id: int, address: str) -> str | None:
    row = conn.execute(
        "SELECT unsub_token FROM personal_recipients WHERE client_id = ? AND address = ?", (client_id, address)
    ).fetchone()
    return row["unsub_token"] if row else None


# Runs, issues and deliveries ----------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, run_date: str, now: datetime) -> int | None:
    """Returns the run id, or None if a run for this date already exists (a weekly run never repeats)."""
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO personal_runs (run_date, started_at, status) VALUES (?, ?, 'running')", (run_date, _iso(now))
            )
    except sqlite3.IntegrityError:
        return None
    return cursor.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, now: datetime) -> None:
    with conn:
        conn.execute("UPDATE personal_runs SET status = ?, finished_at = ? WHERE id = ?", (status, _iso(now), run_id))


def run_for_date(conn: sqlite3.Connection, run_date: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM personal_runs WHERE run_date = ?", (run_date,)).fetchone()


def record_issue(
    conn: sqlite3.Connection, run_id: int, client_id: int, window_start: datetime, window_end: datetime,
    matches: list, now: datetime,
) -> int:
    with conn:
        cursor = conn.execute(
            "INSERT INTO personal_issues (run_id, client_id, window_start, window_end, story_count) VALUES (?, ?, ?, ?, ?)",
            (run_id, client_id, _iso(window_start), _iso(window_end), len(matches)),
        )
        issue_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO personal_issue_items (issue_id, item_id, client_id, score) VALUES (?, ?, ?, ?)",
            [(issue_id, match.item_id, client_id, match.score) for match in matches],
        )
    return issue_id


def already_featured_item_ids(conn: sqlite3.Connection, client_id: int) -> set[int]:
    """Items this client's feed has already carried, so a story is not repeated week after week."""
    return {row["item_id"] for row in conn.execute(
        "SELECT item_id FROM personal_issue_items WHERE client_id = ?", (client_id,)
    )}


def delivery_status(conn: sqlite3.Connection, run_id: int, recipient: str) -> str | None:
    row = conn.execute("SELECT status FROM personal_deliveries WHERE run_id = ? AND recipient = ?", (run_id, recipient)).fetchone()
    return row["status"] if row else None


def mark_delivery(conn: sqlite3.Connection, run_id: int, recipient: str, status: str, at: datetime, error: str | None = None) -> None:
    with conn:
        conn.execute(
            """INSERT INTO personal_deliveries (run_id, recipient, status, attempted_at, sent_at, error)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, recipient) DO UPDATE SET status = excluded.status, attempted_at = excluded.attempted_at,
                   sent_at = excluded.sent_at, error = excluded.error""",
            (run_id, recipient, status, _iso(at), _iso(at) if status == "sent" else None, error),
        )


# Approval requests and the audit log ------------------------------------------------------------


def is_approved(person) -> bool:
    return person["approved_at"] is not None


def approve_person(conn: sqlite3.Connection, person_id: int, now: datetime) -> None:
    with conn:
        conn.execute("UPDATE personal_people SET approved_at = COALESCE(approved_at, ?) WHERE id = ?", (_iso(now), person_id))


def submit_request(conn: sqlite3.Connection, manager_id: int, now: datetime) -> int | None:
    """Opens an approval request. Returns None when one is already waiting."""
    if pending_request_for(conn, manager_id) is not None:
        return None
    with conn:
        cursor = conn.execute(
            "INSERT INTO personal_requests (manager_id, status, submitted_at) VALUES (?, 'pending', ?)",
            (manager_id, _iso(now)),
        )
    return cursor.lastrowid


def pending_request_for(conn: sqlite3.Connection, manager_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM personal_requests WHERE manager_id = ? AND status = 'pending'", (manager_id,)
    ).fetchone()


def request_row(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT r.*, p.email AS manager_email, p.name AS manager_name FROM personal_requests r
           JOIN personal_people p ON p.id = r.manager_id WHERE r.id = ?""",
        (request_id,),
    ).fetchone()


def pending_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.*, p.email AS manager_email, p.name AS manager_name FROM personal_requests r
           JOIN personal_people p ON p.id = r.manager_id WHERE r.status = 'pending' ORDER BY r.submitted_at"""
    ).fetchall()


def decide_request(conn: sqlite3.Connection, request_id: int, *, approve: bool, by: str, reason: str,
                   now: datetime) -> sqlite3.Row | None:
    """Approve or reject a pending request. The first decision wins: a second one returns None."""
    row = request_row(conn, request_id)
    if row is None or row["status"] != "pending":
        return None
    status = "approved" if approve else "rejected"
    with conn:
        cursor = conn.execute(
            "UPDATE personal_requests SET status = ?, decided_at = ?, decided_by = ?, reason = ? WHERE id = ? AND status = 'pending'",
            (status, _iso(now), by, reason, request_id),
        )
        if cursor.rowcount != 1:
            return None
        if approve:
            conn.execute(
                "UPDATE personal_people SET approved_at = COALESCE(approved_at, ?) WHERE id = ?", (_iso(now), row["manager_id"])
            )
    return request_row(conn, request_id)


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str, detail: str = "", *, now: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO personal_audit (at, actor, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (_iso(now), actor, action, target, detail[:500]),
        )


def recent_audit(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM personal_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# Shared admin login ---------------------------------------------------------------------------------

SHARED_EMAIL = "shared-admin"  # a label, not an address: the shared login is never emailed
SHARED_USERNAME_KEY = "shared_admin_username"
SHARED_PASSWORD_KEY = "shared_admin_password"
DEFAULT_SHARED_USERNAME = "admin"
MIN_PASSWORD_LENGTH = 12
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, rounds, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    return hmac.compare_digest(digest.hex(), digest_hex)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM personal_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO personal_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def shared_username(conn: sqlite3.Connection) -> str:
    return get_setting(conn, SHARED_USERNAME_KEY) or DEFAULT_SHARED_USERNAME


def shared_password_set(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, SHARED_PASSWORD_KEY) is not None


def set_shared_password(conn: sqlite3.Connection, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters")
    set_setting(conn, SHARED_PASSWORD_KEY, hash_password(password))


def check_shared_login(conn: sqlite3.Connection, username: str, password: str) -> bool:
    if not shared_password_set(conn):
        return False
    username_ok = hmac.compare_digest(username.encode("utf-8"), shared_username(conn).encode("utf-8"))
    password_ok = verify_password(password, get_setting(conn, SHARED_PASSWORD_KEY))
    return username_ok and password_ok


def ensure_shared_admin(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row:
    """The admin identity used by shared-login sessions. Its actions are recorded under this name."""
    with conn:
        conn.execute(
            """INSERT INTO personal_people (email, name, role, active, created_at, approved_at, shared)
               VALUES (?, 'Shared admin login', 'admin', 1, ?, ?, 1)
               ON CONFLICT(email) DO UPDATE SET role = 'admin', active = 1, shared = 1,
                   approved_at = COALESCE(personal_people.approved_at, excluded.approved_at)""",
            (SHARED_EMAIL, _iso(now), _iso(now)),
        )
    return conn.execute("SELECT * FROM personal_people WHERE email = ?", (SHARED_EMAIL,)).fetchone()
