"""
Loom Coordinator
----------------
Runs on the persistent (always-on) node and:
  - Tracks registered devices (nodes) and their live stats via heartbeats
  - Queues and assigns jobs to nodes based on capability tags
  - Serves a password-gated web dashboard showing node status/CPU/RAM

Auth model:
  - Each node authenticates with its own API key (issued via
    scripts/gen_api_key.py, stored in the devices table). A leaked key
    compromises exactly one node, and can be rotated independently.
  - The human-facing dashboard is gated by a single admin password (hashed in
    config.yaml) behind a signed Flask session cookie.

Run for real with gunicorn (see docs/PI_SETUP.md); `python app.py` is the
development server only.
"""

import os
import re
import sys
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from functools import wraps

import secrets
import yaml
from flask import (
    Flask, request, jsonify, session, redirect, url_for, render_template,
    g, abort,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from db import connect, init_db, db_path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("LOOM_CONFIG", os.path.join(BASE_DIR, "config.yaml"))

AGENT_API_VERSION = 2

# A node with no heartbeat in this many seconds is shown as offline. The agent
# heartbeats every 10s, so this tolerates two consecutive misses.
ONLINE_THRESHOLD_SECONDS = 35

# A job whose agent has stopped checking in for this long is marked 'lost'.
# Generous, because it must exceed the agent heartbeat interval by a wide
# margin -- a slow network should not orphan a job that is running fine.
JOB_LOST_AFTER_SECONDS = 120
REAPER_INTERVAL_SECONDS = 30

# Login throttling: after this many failures from one IP, lock that IP out for
# the cooldown. The dashboard has a single shared password, so unlimited
# guessing is the realistic attack.
LOGIN_MAX_FAILURES = 5
LOGIN_COOLDOWN_SECONDS = 300

MAX_COMMAND_LENGTH = 4000
MAX_LOG_LENGTH = 100_000
VALID_TAG_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")

log = logging.getLogger("loom.coordinator")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REQUIRED_CONFIG = [
    ("dashboard", "secret_key"),
    ("dashboard", "password_hash"),
]

PLACEHOLDERS = ("REPLACE_ME", "REPLACE_ME_WITH_RANDOM_HEX", "REPLACE_ME_WITH_WERKZEUG_HASH")


def load_config(path=CONFIG_PATH):
    if not os.path.exists(path):
        raise SystemExit(
            f"[loom] Missing {path}\n"
            f"       Copy config.example.yaml to config.yaml and fill it in:\n"
            f"         cp config.example.yaml config.yaml\n"
            f"         python ../scripts/gen_api_key.py --dashboard-secret"
        )
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}

    # Fail loudly at boot rather than with a KeyError on the first request, and
    # refuse to start on the example placeholders -- a coordinator running with
    # a known secret_key has forgeable session cookies.
    for section, key in REQUIRED_CONFIG:
        value = (cfg.get(section) or {}).get(key)
        if not value:
            raise SystemExit(f"[loom] config.yaml is missing required key: {section}.{key}")
        if str(value) in PLACEHOLDERS:
            raise SystemExit(
                f"[loom] config.yaml still has the example placeholder for "
                f"{section}.{key}\n"
                f"       Generate real values: python ../scripts/gen_api_key.py --dashboard-secret"
            )

    cfg.setdefault("server", {})
    return cfg


CONFIG = load_config()

app = Flask(__name__)
app.secret_key = CONFIG["dashboard"]["secret_key"]

# Loom is designed to sit behind a reverse proxy, and may be mounted under a
# path prefix. Without this, url_for() generates http:// links.
#
# proxy_hops must match how many proxies actually sit in front of this app,
# because ProxyFix reads the Nth-from-last X-Forwarded-For entry. Get it wrong
# and every request appears to come from the nearest proxy instead of the real
# client -- which silently turns the per-IP login throttle below into a single
# global bucket, so one device's failed logins lock out every other device.
#
#   1 = one proxy (nginx alone, or a tunnel alone)
#   2 = Cloudflare Tunnel -> nginx  (the docs/PI_SETUP.md setup, and the
#       default here, since nginx appends via $proxy_add_x_forwarded_for)
#
# Verify with the /whoami endpoint after any change to your proxy chain.
PROXY_HOPS = int(CONFIG.get("server", {}).get("proxy_hops", 2))
app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=PROXY_HOPS, x_proto=1, x_host=1, x_prefix=1
)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Off by default so a plain-HTTP LAN test still works; turn it on in
    # config.yaml once you are serving over HTTPS (the tunnel does this).
    SESSION_COOKIE_SECURE=bool(CONFIG["dashboard"].get("secure_cookie", False)),
    PERMANENT_SESSION_LIFETIME=timedelta(
        days=int(CONFIG["dashboard"].get("session_days", 7))
    ),
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


# ---------------------------------------------------------------------------
# Database plumbing
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    """Parse a stored timestamp, tolerating rows written before timestamps
    were consistently timezone-aware."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def age_seconds(value):
    dt = parse_iso(value)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def is_online(last_seen_iso):
    age = age_seconds(last_seen_iso)
    return age is not None and age <= ONLINE_THRESHOLD_SECONDS


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_login_failures = {}          # ip -> (failure_count, first_failure_monotonic)
_login_lock = threading.Lock()


def _login_blocked(ip):
    with _login_lock:
        entry = _login_failures.get(ip)
        if not entry:
            return 0
        count, first = entry
        elapsed = time.monotonic() - first
        if elapsed > LOGIN_COOLDOWN_SECONDS:
            _login_failures.pop(ip, None)
            return 0
        if count >= LOGIN_MAX_FAILURES:
            return int(LOGIN_COOLDOWN_SECONDS - elapsed)
        return 0


def _record_login_failure(ip):
    with _login_lock:
        count, first = _login_failures.get(ip, (0, time.monotonic()))
        if time.monotonic() - first > LOGIN_COOLDOWN_SECONDS:
            count, first = 0, time.monotonic()
        _login_failures[ip] = (count + 1, first)


def _clear_login_failures(ip):
    with _login_lock:
        _login_failures.pop(ip, None)


def csrf_token():
    """One token per session, checked on every state-changing request.

    SameSite=Lax already blocks cross-site form POSTs in current browsers;
    this is the belt to that suspenders, and costs nothing.
    """
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            # An expired session on a background poll should read as 401 to the
            # dashboard JS, not as a redirect it would try to render as JSON.
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthenticated"}), 401
            return redirect(url_for("login", next=request.path))
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            sent = request.headers.get("X-Loom-CSRF") or request.form.get("csrf")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                return jsonify({"error": "bad or missing CSRF token"}), 403
        return f(*args, **kwargs)
    return wrapper


def require_device_api_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if not key:
            return jsonify({"error": "missing X-API-Key header"}), 401
        device = get_db().execute(
            "SELECT * FROM devices WHERE api_key = ?", (key,)
        ).fetchone()
        if not device:
            log.warning("rejected agent auth from %s", request.remote_addr)
            return jsonify({"error": "invalid API key"}), 403
        g.device = device
        return f(*args, **kwargs)
    return wrapper


def json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="expected a JSON object body")
    return data


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr or "unknown"

    if request.method == "POST":
        blocked_for = _login_blocked(ip)
        if blocked_for:
            return render_template(
                "login.html",
                error=f"Too many failed attempts. Try again in {blocked_for}s.",
                csrf=csrf_token(),
            ), 429

        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            return render_template(
                "login.html", error="Session expired, try again.", csrf=csrf_token()
            ), 400

        password = request.form.get("password", "")
        if check_password_hash(CONFIG["dashboard"]["password_hash"], password):
            _clear_login_failures(ip)
            # Drop the pre-login session so a fixated cookie cannot be reused.
            session.clear()
            session["admin"] = True
            session.permanent = True
            csrf_token()
            log.info("dashboard login from %s", ip)
            nxt = request.args.get("next", "")
            # Only ever redirect to a local path -- never to an attacker-supplied host.
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("dashboard"))

        _record_login_failure(ip)
        log.warning("failed dashboard login from %s", ip)
        return render_template(
            "login.html", error="Incorrect password", csrf=csrf_token()
        ), 401

    return render_template("login.html", error=None, csrf=csrf_token())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@require_admin
def dashboard():
    return render_template("dashboard.html", csrf=csrf_token())


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for systemd/uptime checks. Deliberately
    exposes no node names, counts, or stats."""
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:
        return jsonify({"ok": False}), 503
    return jsonify({"ok": True, "agent_api_version": AGENT_API_VERSION})


@app.route("/whoami")
@require_admin
def whoami():
    """What the coordinator thinks your IP is, for checking proxy_hops.

    If `remote_addr` comes back as 127.0.0.1 or your proxy's address rather
    than your actual client IP, proxy_hops is wrong and the login throttle is
    lumping every client into one bucket.
    """
    return jsonify({
        "remote_addr": request.remote_addr,
        "proxy_hops": PROXY_HOPS,
        "x_forwarded_for": request.headers.get("X-Forwarded-For"),
        "cf_connecting_ip": request.headers.get("CF-Connecting-IP"),
        "looks_correct": request.remote_addr not in ("127.0.0.1", "::1"),
    })


def _device_row_to_dict(r):
    return {
        "id": r["id"],
        "name": r["name"],
        "platform": r["platform"],
        "tags": [t for t in (r["tags"] or "").split(",") if t],
        "persistent": bool(r["persistent"]),
        "online": is_online(r["last_seen"]),
        "last_seen": r["last_seen"],
        "last_seen_age": age_seconds(r["last_seen"]),
        "hostname": r["hostname"],
        "agent_version": r["agent_version"],
        "cpu_percent": r["cpu_percent"],
        "ram_percent": r["ram_percent"],
        "ram_used_gb": r["ram_used_gb"],
        "ram_total_gb": r["ram_total_gb"],
        "disk_percent": r["disk_percent"],
        "uptime_seconds": r["uptime_seconds"],
        "running_jobs": r["running_jobs"] or 0,
    }


@app.route("/api/dashboard/nodes")
@require_admin
def dashboard_nodes():
    rows = get_db().execute(
        "SELECT * FROM devices ORDER BY persistent DESC, name ASC"
    ).fetchall()
    return jsonify([_device_row_to_dict(r) for r in rows])


@app.route("/api/dashboard/jobs")
@require_admin
def dashboard_jobs():
    limit = min(int(request.args.get("limit", 100)), 500)
    rows = get_db().execute(
        """
        SELECT jobs.id, jobs.command, jobs.status, jobs.target_tag, jobs.source,
               jobs.created_at, jobs.started_at, jobs.finished_at,
               jobs.exit_code, jobs.result,
               devices.name AS assigned_name
        FROM jobs LEFT JOIN devices ON jobs.assigned_device_id = devices.id
        ORDER BY jobs.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/dashboard/jobs/<int:job_id>")
@require_admin
def dashboard_job_detail(job_id):
    """Full record including the captured log -- kept out of the list endpoint
    so the 4-second dashboard refresh does not ship every job's output."""
    row = get_db().execute(
        """
        SELECT jobs.*, devices.name AS assigned_name
        FROM jobs LEFT JOIN devices ON jobs.assigned_device_id = devices.id
        WHERE jobs.id = ?
        """,
        (job_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "no such job"}), 404
    return jsonify(dict(row))


@app.route("/api/dashboard/jobs/submit", methods=["POST"])
@require_admin
def submit_job():
    data = json_body()
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "command is required"}), 400
    if len(command) > MAX_COMMAND_LENGTH:
        return jsonify({"error": f"command exceeds {MAX_COMMAND_LENGTH} characters"}), 400

    target_tag = (data.get("target_tag") or "").strip() or None
    if target_tag and not VALID_TAG_RE.match(target_tag):
        return jsonify({"error": "tag must be 1-32 chars of [a-zA-Z0-9_.-]"}), 400

    target_device_id = data.get("target_device_id")
    if target_device_id in ("", None):
        target_device_id = None
    else:
        # The dashboard sends form values as strings; storing "3" here would
        # never match the integer device id at poll time and the job would
        # silently hang as pending forever.
        try:
            target_device_id = int(target_device_id)
        except (TypeError, ValueError):
            return jsonify({"error": "target_device_id must be an integer"}), 400
        exists = get_db().execute(
            "SELECT 1 FROM devices WHERE id = ?", (target_device_id,)
        ).fetchone()
        if not exists:
            return jsonify({"error": "no such device"}), 400

    return _insert_job(command, target_tag, target_device_id, source="dashboard")


def _insert_job(command, target_tag, target_device_id, source):
    db = get_db()
    cur = db.execute(
        "INSERT INTO jobs (command, target_tag, target_device_id, status, source, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (command, target_tag, target_device_id, source, now_iso()),
    )
    log.info("queued job %s (source=%s): %s", cur.lastrowid, source, command[:120])
    return jsonify({"job_id": cur.lastrowid}), 201


@app.route("/api/dashboard/jobs/<int:job_id>/cancel", methods=["POST"])
@require_admin
def cancel_job(job_id):
    """Cancel a job. A pending job is cancelled outright; one already handed to
    an agent is flagged so the agent kills it at its next check-in."""
    db = get_db()
    row = db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return jsonify({"error": "no such job"}), 404
    if row["status"] in ("done", "failed", "cancelled", "lost"):
        return jsonify({"error": f"job is already {row['status']}"}), 409

    db.execute(
        "UPDATE jobs SET status = 'cancelled', finished_at = ?, "
        "result = COALESCE(result, 'cancelled from dashboard') WHERE id = ?",
        (now_iso(), job_id),
    )
    log.info("cancelled job %s (was %s)", job_id, row["status"])
    return jsonify({"ok": True, "was": row["status"]})


# ---------------------------------------------------------------------------
# Device-facing API
# ---------------------------------------------------------------------------

def _clamp(value, low, high):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, value))


@app.route("/api/heartbeat", methods=["POST"])
@require_device_api_key
def heartbeat():
    data = json_body()
    db = get_db()
    device_id = g.device["id"]

    # Stats come from a remote agent, so clamp rather than trust: a bad value
    # would otherwise render as a 4000%-wide bar in the dashboard.
    db.execute(
        """
        UPDATE devices SET
            last_seen = ?, hostname = ?, agent_version = ?,
            cpu_percent = ?, ram_percent = ?, ram_used_gb = ?, ram_total_gb = ?,
            disk_percent = ?, uptime_seconds = ?, running_jobs = ?,
            platform = COALESCE(?, platform)
        WHERE id = ?
        """,
        (
            now_iso(),
            str(data.get("hostname") or "")[:120] or None,
            str(data.get("agent_version") or "")[:32] or None,
            _clamp(data.get("cpu_percent"), 0, 100),
            _clamp(data.get("ram_percent"), 0, 100),
            _clamp(data.get("ram_used_gb"), 0, 1e6),
            _clamp(data.get("ram_total_gb"), 0, 1e6),
            _clamp(data.get("disk_percent"), 0, 100),
            data.get("uptime_seconds") if isinstance(data.get("uptime_seconds"), int) else None,
            len(data.get("running_jobs") or []),
            data.get("platform"),
            device_id,
        ),
    )

    # Keep the jobs this agent says it is still working on out of the reaper's
    # reach, and tell the agent which of them the dashboard has cancelled.
    running = [j for j in (data.get("running_jobs") or []) if isinstance(j, int)]
    cancelled = []
    if running:
        placeholders = ",".join("?" for _ in running)
        db.execute(
            f"UPDATE jobs SET heartbeat_at = ? WHERE assigned_device_id = ? "
            f"AND id IN ({placeholders})",
            (now_iso(), device_id, *running),
        )
        cancelled = [
            r["id"] for r in db.execute(
                f"SELECT id FROM jobs WHERE assigned_device_id = ? AND status = 'cancelled' "
                f"AND id IN ({placeholders})",
                (device_id, *running),
            ).fetchall()
        ]

    return jsonify({"ok": True, "cancel": cancelled, "agent_api_version": AGENT_API_VERSION})


@app.route("/api/jobs/poll", methods=["GET"])
@require_device_api_key
def poll_jobs():
    """A device asks: 'is there a job for me?'

    Matching, oldest job first:
      1. Jobs explicitly targeted at this device id
      2. Jobs targeted at a tag this device carries
      3. Untargeted jobs -- only for the device marked persistent, so ownerless
         work always has exactly one home

    The select-then-claim runs inside a BEGIN IMMEDIATE transaction. The
    previous version read a pending row and updated it in two separate
    statements, so two agents polling at the same moment could both see the
    same row and both run the job. BEGIN IMMEDIATE takes the write lock before
    the read, which serialises the whole claim without depending on a SQLite
    version new enough for UPDATE ... RETURNING.
    """
    db = get_db()
    device = g.device
    device_tags = [t for t in (device["tags"] or "").split(",") if t]

    conditions = ["target_device_id = ?"]
    params = [device["id"]]

    if device_tags:
        placeholders = ",".join("?" for _ in device_tags)
        conditions.append(
            f"(target_tag IS NOT NULL AND target_tag != '' AND target_tag IN ({placeholders}))"
        )
        params.extend(device_tags)

    if device["persistent"]:
        conditions.append(
            "(COALESCE(target_tag, '') = '' AND target_device_id IS NULL)"
        )

    select_sql = (
        f"SELECT id, command FROM jobs "
        f"WHERE status = 'pending' AND ({' OR '.join(conditions)}) "
        f"ORDER BY id ASC LIMIT 1"
    )

    db.execute("BEGIN IMMEDIATE")
    try:
        job = db.execute(select_sql, params).fetchone()
        if not job:
            db.execute("COMMIT")
            return jsonify({"job": None})
        stamp = now_iso()
        db.execute(
            "UPDATE jobs SET status = 'assigned', assigned_device_id = ?, "
            "assigned_at = ?, heartbeat_at = ? WHERE id = ?",
            (device["id"], stamp, stamp, job["id"]),
        )
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise

    log.info("assigned job %s to %s", job["id"], device["name"])
    return jsonify({"job": {"id": job["id"], "command": job["command"]}})


@app.route("/api/jobs/submit", methods=["POST"])
@require_device_api_key
def agent_submit_job():
    """Lets an agent queue work -- used by file-watch triggers configured with
    `mode: queue`.

    The agent previously posted to the dashboard endpoint, which is session
    gated, so those submissions were silently answered with a redirect to the
    login page and never queued at all.
    """
    data = json_body()
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "command is required"}), 400
    if len(command) > MAX_COMMAND_LENGTH:
        return jsonify({"error": f"command exceeds {MAX_COMMAND_LENGTH} characters"}), 400

    target_tag = (data.get("target_tag") or "").strip() or None
    if target_tag and not VALID_TAG_RE.match(target_tag):
        return jsonify({"error": "invalid tag"}), 400

    target_device_id = data.get("target_device_id")
    if target_device_id in ("", None):
        target_device_id = None
    else:
        try:
            target_device_id = int(target_device_id)
        except (TypeError, ValueError):
            return jsonify({"error": "target_device_id must be an integer"}), 400

    return _insert_job(
        command, target_tag, target_device_id,
        source=f"trigger:{g.device['name']}",
    )


@app.route("/api/jobs/<int:job_id>/running", methods=["POST"])
@require_device_api_key
def job_running(job_id):
    db = get_db()
    cur = db.execute(
        "UPDATE jobs SET status = 'running', started_at = ?, heartbeat_at = ? "
        "WHERE id = ? AND assigned_device_id = ? AND status = 'assigned'",
        (now_iso(), now_iso(), job_id, g.device["id"]),
    )
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "job not assigned to this device"}), 409
    return jsonify({"ok": True})


@app.route("/api/jobs/<int:job_id>/complete", methods=["POST"])
@require_device_api_key
def job_complete(job_id):
    data = json_body()
    db = get_db()

    # A cancelled or reaped job must not be resurrected into 'done' by a late
    # report from an agent that finished after the dashboard gave up on it.
    row = db.execute(
        "SELECT status FROM jobs WHERE id = ? AND assigned_device_id = ?",
        (job_id, g.device["id"]),
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "job not assigned to this device"}), 409
    if row["status"] == "cancelled":
        return jsonify({"ok": True, "note": "job was cancelled; result discarded"})

    exit_code = data.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = None
    status = "done" if data.get("success") else "failed"

    db.execute(
        "UPDATE jobs SET status = ?, exit_code = ?, result = ?, log = ?, finished_at = ? "
        "WHERE id = ? AND assigned_device_id = ?",
        (
            status,
            exit_code,
            str(data.get("result") or "")[:500],
            str(data.get("log") or "")[:MAX_LOG_LENGTH],
            now_iso(),
            job_id,
            g.device["id"],
        ),
    )
    log.info("job %s finished on %s: %s", job_id, g.device["name"], status)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Reaper: rescue jobs whose agent died mid-run
# ---------------------------------------------------------------------------

def reap_lost_jobs():
    """Without this, an agent that is unplugged mid-job leaves that job stuck in
    'running' forever, and the dashboard shows work that will never finish."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=JOB_LOST_AFTER_SECONDS)
    ).isoformat()
    conn = connect()
    try:
        cur = conn.execute(
            """
            UPDATE jobs SET status = 'lost', finished_at = ?,
                            result = 'agent stopped reporting'
            WHERE status IN ('assigned', 'running')
              AND COALESCE(heartbeat_at, assigned_at, created_at) < ?
            """,
            (now_iso(), cutoff),
        )
        if cur.rowcount:
            log.warning("marked %d job(s) lost (agent stopped reporting)", cur.rowcount)
    except Exception as e:
        log.error("reaper failed: %s", e)
    finally:
        conn.close()


def start_reaper():
    def loop():
        while True:
            time.sleep(REAPER_INTERVAL_SECONDS)
            reap_lost_jobs()

    threading.Thread(target=loop, daemon=True, name="loom-reaper").start()


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

def bootstrap():
    """Runs on import so that the app works identically under gunicorn and
    under `python app.py` -- the schema used to be created only in the
    __main__ block, so a gunicorn-managed coordinator started against an empty
    database and failed on the first query."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_db()
    log.info("loom coordinator ready (db=%s)", db_path())
    start_reaper()


bootstrap()


if __name__ == "__main__":
    server = CONFIG.get("server", {})
    debug = bool(server.get("debug", False))
    if debug:
        print(
            "[loom] WARNING: debug=true exposes an interactive console to anyone "
            "who can reach this port. Never leave this on for a tunnelled "
            "coordinator.",
            file=sys.stderr,
        )
    app.run(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 5055)),
        debug=debug,
        threaded=True,
    )
