"""Hosting mode (e.g. Railway): one always-on process that runs the schedule and a small admin page.

Schedule, in newsletter.timezone (settings.yaml):
    daily at fetch_time      fetch the news
    at every send slot       build the issue, then send it to production recipients if PROD_SEND_ENABLED=true

Admin page, only when ADMIN_TOKEN is set (at least 32 characters). The browser asks for a user name and
password: any user name, and the token as the password. Scripts send "Authorization: Bearer <token>".
    GET  /              status, with buttons to send a test email or build a preview now
    POST /run/test      fetch, build and send a [TEST] copy to TEST_RECIPIENTS only
    POST /run/preview   build the issue from the news already stored, no email
    GET  /preview       the latest built issue
    GET  /health        liveness check for the host (no sign-in)
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import html
import json
import logging
import os
import signal
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import cli, db
from .config import Config, ConfigError
from .mail_profiles import PRODUCTION, TEST, load_profile, production_sending_enabled
from .mailer import MailError

log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
TICK_SECONDS = 30
CATCH_UP = timedelta(hours=3)  # a send slot missed while the service was down still runs within this window
SHUTDOWN_WAIT_SECONDS = 90
ADMIN_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
PREVIEW_CSP = "default-src 'none'; img-src data: https:; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"


# Schedule -----------------------------------------------------------------------------------------


@dataclass
class ScheduleState:
    last_fetch_date: date | None = None
    production_dates: set[date] = field(default_factory=set)


def due_jobs(now: datetime, cfg: Config, state: ScheduleState, sent_dates: Iterable[date]) -> list[str]:
    """Jobs due at `now` (local, timezone-aware): "fetch" and/or "production"."""
    today = now.date()
    jobs = []
    if now.time() >= _clock(cfg.newsletter.fetch_time) and state.last_fetch_date != today:
        jobs.append("fetch")
    sent = set(sent_dates)
    for slot in cfg.newsletter.send_slots:
        if slot.weekday != today.weekday() or today in state.production_dates or today in sent:
            continue
        slot_time = datetime.combine(today, time(slot.hour, slot.minute), tzinfo=now.tzinfo)
        if slot_time <= now <= slot_time + CATCH_UP:
            jobs.append("production")
            break
    return jobs


def next_slot(now: datetime, cfg: Config) -> datetime:
    upcoming = []
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        for slot in cfg.newsletter.send_slots:
            moment = datetime.combine(day, time(slot.hour, slot.minute), tzinfo=now.tzinfo)
            if day.weekday() == slot.weekday and moment > now:
                upcoming.append(moment)
    return min(upcoming)


def _clock(value: str) -> time:
    return datetime.strptime(value.strip(), "%H:%M").time()


# Jobs -----------------------------------------------------------------------------------------------


@dataclass
class JobRecord:
    kind: str
    started: datetime
    finished: datetime | None = None
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)

    @property
    def result(self) -> str:
        if self.finished is None:
            return "running"
        return "ok" if self.exit_code == 0 else "problems (see log)"


class _Capture(logging.Handler):
    """Keeps this app's log lines while a job runs, for the admin page."""

    def __init__(self, record: JobRecord, limit: int = 400) -> None:
        super().__init__(logging.INFO)
        self.job, self.limit = record, limit
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("rankuno_brief") and len(self.job.lines) < self.limit:
            self.job.lines.append(self.format(record))


class JobRunner:
    """Runs one job at a time in a background thread, each with its own database connection."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.current: JobRecord | None = None
        self.history: deque[JobRecord] = deque(maxlen=10)
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    def start(self, kind: str, job: Callable) -> bool:
        with self._lock:
            if self.current is not None:
                return False
            record = self.current = JobRecord(kind=kind, started=datetime.now(timezone.utc))
            self._idle.clear()
        threading.Thread(target=self._run, args=(record, job), name=f"job-{kind}", daemon=True).start()
        return True

    def wait(self, timeout: float) -> bool:
        return self._idle.wait(timeout)

    def _run(self, record: JobRecord, job: Callable) -> None:
        capture = _Capture(record)
        logging.getLogger().addHandler(capture)
        log.info("Job started: %s", record.kind)
        conn = db.connect(self.cfg.db_path)
        try:
            record.exit_code = job(conn)
        except (ConfigError, MailError) as exc:
            log.error("%s", exc)
            record.exit_code = 1
        except Exception:  # noqa: BLE001 - a crashed job must never take the service down
            log.exception("Job %s crashed", record.kind)
            record.exit_code = 1
        finally:
            conn.close()
            record.finished = datetime.now(timezone.utc)
            log.info("Job finished: %s (%s)", record.kind, record.result)
            logging.getLogger().removeHandler(capture)
            with self._lock:
                self.current = None
                self.history.appendleft(record)
                self._idle.set()


class App:
    def __init__(self, cfg: Config, token: str) -> None:
        self.cfg = cfg
        self.token = token
        self.runner = JobRunner(cfg)
        self.state = ScheduleState()

    # Jobs the admin page and the schedule start
    def start_test(self) -> bool:
        return self.runner.start("test email", lambda conn: cli.run_pipeline(self.cfg, conn, test=True, fetch=True))

    def start_preview(self) -> bool:
        return self.runner.start("preview build", lambda conn: cli.run_pipeline(self.cfg, conn, test=False, fetch=False, send=False))

    def _production(self, conn) -> int:
        if production_sending_enabled():
            return cli.run_pipeline(self.cfg, conn, test=False, fetch=False)
        log.info("Production sending is off (PROD_SEND_ENABLED): the issue is built but not sent")
        return cli.run_pipeline(self.cfg, conn, test=False, fetch=False, send=False)

    def restore_state(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            last = db.last_fetch_run(conn)
        finally:
            conn.close()
        if last:
            self.state.last_fetch_date = db.from_iso(last["started_at"]).astimezone(self.cfg.newsletter.timezone).date()

    def tick(self, now: datetime) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            sent_dates = {date.fromisoformat(value) for value in db.sent_issue_dates(conn)}
        finally:
            conn.close()
        for job in due_jobs(now, self.cfg, self.state, sent_dates):
            if job == "fetch" and self.runner.start("scheduled fetch", lambda conn: cli.cmd_fetch(self.cfg, conn, argparse.Namespace())):
                self.state.last_fetch_date = now.date()
            elif job == "production" and self.runner.start("scheduled issue", self._production):
                self.state.production_dates.add(now.date())


# HTTP -----------------------------------------------------------------------------------------------


def authorized(header: str | None, token: str) -> bool:
    """Bearer token, or HTTP Basic auth with the token as the password. Constant-time comparison."""
    if not token or not header:
        return False
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() == "bearer":
        supplied = value.strip()
    elif scheme.lower() == "basic":
        try:
            supplied = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")[2]
        except (binascii.Error, UnicodeDecodeError):
            return False
    else:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8"))


def same_origin(headers) -> bool:
    """Browsers send Origin (or Referer) with form posts; it must be this site, so other sites cannot trigger jobs."""
    source = headers.get("Origin") or headers.get("Referer")
    return bool(source) and source != "null" and urlsplit(source).netloc == headers.get("Host")


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "RankUnoBrief"
    sys_version = ""

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            return self._send(200, json.dumps({"status": "ok"}), "application/json")
        if not self._check_auth():
            return
        if path == "/":
            return self._send(200, status_page(self.app), "text/html; charset=utf-8", csp=ADMIN_CSP)
        if path == "/preview":
            body = preview_page(self.app.cfg)
            if body is None:
                return self._send(404, "No issue has been built yet.", "text/plain; charset=utf-8")
            return self._send(200, body, "text/html; charset=utf-8", csp=PREVIEW_CSP)
        return self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._check_auth():
            return
        bearer = (self.headers.get("Authorization") or "").lower().startswith("bearer ")
        if not bearer and not same_origin(self.headers):
            return self._send(403, "Refused: the request did not come from this admin page.", "text/plain; charset=utf-8")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10_000:
            return self._send(413, "Request too large", "text/plain; charset=utf-8")
        self.rfile.read(length)

        actions = {"/run/test": self.app.start_test, "/run/preview": self.app.start_preview}
        if path not in actions:
            return self._send(404, "Not found", "text/plain; charset=utf-8")
        started = actions[path]()
        if bearer:
            status = 202 if started else 409
            return self._send(status, json.dumps({"started": started}), "application/json")
        self.send_response(303)
        self.send_header("Location", "/")
        self._security_headers()
        self.end_headers()

    def _check_auth(self) -> bool:
        if not self.app.token:
            self._send(404, "Not found", "text/plain; charset=utf-8")  # admin page switched off
            return False
        if authorized(self.headers.get("Authorization"), self.app.token):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="The RankUno Brief admin", charset="UTF-8"')
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, status: int, body: str, content_type: str, csp: str | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self._security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature defined by BaseHTTPRequestHandler
        log.debug("%s %s", self.address_string(), format % args)


# Pages ----------------------------------------------------------------------------------------------


def status_page(app: App) -> str:
    cfg, esc = app.cfg, html.escape
    tz = cfg.newsletter.timezone
    now = datetime.now(tz)
    conn = db.connect(cfg.db_path)
    try:
        issue, fetch_run = db.latest_built_issue(conn), db.last_fetch_run(conn)
    finally:
        conn.close()
    running = app.runner.current

    def local(value: str | datetime | None) -> str:
        if not value:
            return "never"
        moment = db.from_iso(value) if isinstance(value, str) else value
        return moment.astimezone(tz).strftime("%a %d %b %Y, %H:%M")

    def profile_block(name: str) -> tuple[str, bool]:
        try:
            return "\n".join(load_profile(name, cfg).describe()), True
        except ConfigError as exc:
            return str(exc), False

    production_text, _ = profile_block(PRODUCTION)
    test_text, test_ready = profile_block(TEST)
    busy = running is not None
    disabled = " disabled" if busy else ""

    rows = []
    for record in ([running] if running else []) + list(app.runner.history):
        duration = f"{(record.finished - record.started).seconds}s" if record.finished else "…"
        rows.append(f"<tr><td>{esc(record.kind)}</td><td>{esc(local(record.started))}</td><td>{duration}</td><td>{esc(record.result)}</td></tr>")
    latest_job = running or (app.runner.history[0] if app.runner.history else None)
    job_log = esc("\n".join(latest_job.lines)) if latest_job else "No jobs have run since the service started."

    if issue:
        issue_html = (
            f"No. {issue['number']} for {esc(issue['issue_date'])}, {esc(issue['status'])}, {issue['story_count']} stories, "
            f"built {esc(local(issue['built_at']))}. <a href=\"/preview\">View the issue</a>"
        )
    else:
        issue_html = "No issue has been built yet."
    fetch_html = (
        f"{esc(local(fetch_run['started_at']))}: {fetch_run['sources_ok'] or 0} sources ok, "
        f"{fetch_run['sources_failed'] or 0} failed, {fetch_run['items_new'] or 0} new items"
        if fetch_run
        else "never"
    )
    refresh = '<meta http-equiv="refresh" content="10">' if busy else ""
    test_button = (
        f'<form method="post" action="/run/test"><button{disabled}>Send test email now</button></form>'
        if test_ready
        else "<p class=\"warn\">Set the TEST_ mail settings and TEST_RECIPIENTS to send test emails.</p>"
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>The RankUno Brief · Admin</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; color: #262626; background: #F2F2F2; margin: 0; padding: 24px 16px; }}
  main {{ max-width: 860px; margin: 0 auto; background: #fff; border-top: 5px solid #DF212A; padding: 24px 28px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }} h2 {{ font-size: 13px; letter-spacing: 1px; text-transform: uppercase; margin: 28px 0 8px; }}
  p, td {{ font-size: 14px; line-height: 21px; }} .muted {{ color: #707070; }} .warn {{ color: #A1161D; }}
  pre {{ background: #F7F7F7; padding: 12px; font-size: 12px; line-height: 18px; overflow-x: auto; white-space: pre-wrap; }}
  form {{ display: inline-block; margin: 0 8px 8px 0; }}
  button {{ background: #DF212A; color: #fff; border: 0; padding: 10px 16px; font-size: 14px; font-weight: bold; cursor: pointer; }}
  button.secondary {{ background: #262626; }} button[disabled] {{ background: #BDBDBD; cursor: default; }}
  table {{ border-collapse: collapse; width: 100%; }} td {{ border-bottom: 1px solid #E6E6E6; padding: 6px 8px 6px 0; }}
  a {{ color: #DF212A; }}
</style></head><body><main>
<h1>The RankUno Brief</h1>
<p class="muted">Admin · {esc(now.strftime("%a %d %b %Y, %H:%M"))} ({esc(str(tz))})</p>

<h2>Run now</h2>
{test_button}
<form method="post" action="/run/preview"><button class="secondary"{disabled}>Build preview (no email)</button></form>
<p class="muted">A test email fetches the latest news, builds the issue and sends a [TEST] copy to TEST_RECIPIENTS only.
Nothing is marked as sent. {"<strong>A job is running; this page refreshes itself.</strong>" if busy else ""}</p>

<h2>Schedule</h2>
<p>News fetch daily at {esc(cfg.newsletter.fetch_time)}. Next issue: {esc(local(next_slot(now, cfg)))}.
Production sending is <strong>{"ON" if production_sending_enabled() else "OFF"}</strong>.</p>

<h2>Latest issue</h2><p>{issue_html}</p>
<h2>Last news fetch</h2><p>{fetch_html}</p>

<h2>Jobs since start</h2>
<table>{"".join(rows) or '<tr><td class="muted">None yet</td></tr>'}</table>
<h2>Log of the latest job</h2><pre>{job_log}</pre>

<h2>Mail settings: production</h2><pre>{esc(production_text)}</pre>
<h2>Mail settings: test</h2><pre>{esc(test_text)}</pre>
</main></body></html>"""


def preview_page(cfg: Config) -> str | None:
    """The latest built issue as sent, with its embedded logos inlined so a browser can show them."""
    conn = db.connect(cfg.db_path)
    try:
        issue = db.latest_built_issue(conn)
    finally:
        conn.close()
    if issue is None:
        return None
    try:
        body = cli.issue_file(cfg, issue["html_path"]).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    for content_id, path in cli.issue_images(cfg, issue, body).items():
        if not path.is_file():
            continue
        media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        body = body.replace(f'"cid:{content_id}"', f'"data:{media_type};base64,{data}"')
    return body


# Entry point ----------------------------------------------------------------------------------------


def serve(cfg: Config) -> int:
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if token and len(token) < MIN_TOKEN_LENGTH:
        log.error("ADMIN_TOKEN must be at least %d characters; the admin page stays off", MIN_TOKEN_LENGTH)
        token = ""
    elif not token:
        log.warning("ADMIN_TOKEN is not set: the admin page is off and only /health responds")

    app = App(cfg, token)
    app.restore_state()
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), AdminHandler)
    httpd.daemon_threads = True
    httpd.app = app  # type: ignore[attr-defined]
    stop = threading.Event()

    def schedule_loop() -> None:
        while True:
            try:
                app.tick(datetime.now(cfg.newsletter.timezone))
            except Exception:  # noqa: BLE001 - keep the schedule alive through transient errors
                log.exception("Scheduler tick failed")
            if stop.wait(TICK_SECONDS):
                return

    def request_stop(signum, frame) -> None:
        log.info("Stopping (signal %s)", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    threading.Thread(target=schedule_loop, name="schedule", daemon=True).start()
    log.info(
        "Serving on port %d. News fetch daily at %s; issues at %s (%s); production sending %s; admin page %s.",
        port,
        cfg.newsletter.fetch_time,
        ", ".join(f"{('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[slot.weekday]} {slot.hour:02d}:{slot.minute:02d}" for slot in cfg.newsletter.send_slots),
        cfg.newsletter.timezone,
        "ON" if production_sending_enabled() else "OFF",
        "on" if token else "off",
    )

    while not stop.wait(1):
        pass
    httpd.shutdown()
    if not app.runner.wait(SHUTDOWN_WAIT_SECONDS):
        log.warning("A job was still running at shutdown; an interrupted send is reported as uncertain next time")
    return 0
