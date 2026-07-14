import os
import threading
import time

import requests
from flask import Flask, flash, get_flashed_messages, jsonify, redirect, render_template, request, session, url_for

from history import MAX_ENTRIES, OUTCOME_COOLDOWN, OUTCOME_ERROR, OUTCOME_STARTED, ScanHistory

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

MAX_ATTEMPTS = 3
ATTEMPT_WINDOW = 5 * 60     # 5 minutes
LOCKOUT_SECONDS = 60 * 60   # 1 hour
COOLDOWN_SECONDS = 60 * 60  # 1 hour

# Scan history. Lives on a bind mount (docker-compose: ./data:/data) so it — and
# therefore the cooldown, which is derived from it — survives a container
# restart or an image pull. There is deliberately no in-memory `scan_until`:
# that global reset on every restart and let anyone scan again immediately.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
HISTORY_PATH = os.path.join(DATA_DIR, "scan_history.json")
history = ScanHistory(HISTORY_PATH)

# Serialises the cooldown check -> Jellyfin call -> "started" record sequence.
# The dev server is threaded, so without this two simultaneous presses both pass
# the cooldown check and both kick off a scan.
scan_lock = threading.Lock()

# In-process cooldown safety net. The cooldown's source of truth is the last
# OUTCOME_STARTED row on disk, but disk can fail two ways that must NOT silently
# disable rate limiting (fail-open):
#   * a STARTED record can't be persisted (read-only /data, disk full)  -> _last_started_fallback
#   * the history file can't be read at all (EMFILE/EACCES/EIO)         -> fall back to last good read
# Both let the cooldown survive for the life of the process instead of vanishing.
_state_lock = threading.Lock()
_last_started_fallback = 0.0   # set when a STARTED record fails to persist
_last_started_cache = 0.0      # most recent last_started_at successfully read from disk


def jf_headers():
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"}


def authenticated():
    return session.get("auth") is True


def client_ip():
    """Best-effort client IP.

    X-Forwarded-For is trivially spoofed by anyone who can reach the app, and
    this app is reachable on the LAN — so only believe it when the operator has
    explicitly said there IS a proxy in front (TRUST_PROXY=1).
    """
    if os.environ.get("TRUST_PROXY") == "1":
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded.strip():
            # Assume exactly ONE trusted proxy. It appends the address it
            # actually observed to the RIGHT of any client-supplied value, so
            # the rightmost token is the real peer. The leftmost is fully
            # attacker-controlled (a client can prepend a forged/victim IP) and
            # must never be trusted, even behind a legitimate proxy.
            return forwarded.split(",")[-1].strip()
    return request.remote_addr or ""


def record_press(outcome, error=""):
    """Record a button press. Never lets a history problem break the button."""
    try:
        return history.record(
            outcome,
            ip=client_ip(),
            user_agent=request.headers.get("User-Agent", ""),
            error=error,
        )
    except Exception:  # e.g. read-only /data — log it, but still serve the user
        app.logger.exception("failed to record scan history entry")
        if outcome == OUTCOME_STARTED:
            # The scan really started but we couldn't persist the row the
            # cooldown is derived from. Without this the cooldown would silently
            # cease to exist and the button could be held down to hammer
            # Jellyfin. Keep it alive in-process for the life of this process.
            global _last_started_fallback
            with _state_lock:
                _last_started_fallback = time.time()
        return None


def cooldown_remaining():
    """Seconds left on the cooldown, derived from the last successful scan.

    Fails CLOSED: if the history file can't be read (or a STARTED record
    couldn't be written), we do NOT return 0 and hand out a free scan — we fall
    back to the last known-good scan time, or to a full cooldown if we have no
    baseline at all.
    """
    global _last_started_cache
    try:
        last_started = history.last_started_at()
    except Exception:
        app.logger.exception("failed to read scan history")
        with _state_lock:
            baseline = max(_last_started_cache, _last_started_fallback)
        if baseline <= 0:
            # Never observed a successful scan, and now we can't read the file.
            # Assume a scan just happened rather than risk hammering Jellyfin.
            return float(COOLDOWN_SECONDS)
        return max(0.0, baseline + COOLDOWN_SECONDS - time.time())

    with _state_lock:
        if last_started > _last_started_cache:
            _last_started_cache = last_started
        baseline = max(last_started, _last_started_fallback)
    return max(0.0, baseline + COOLDOWN_SECONDS - time.time())


def lockout_remaining():
    locked_until = session.get("locked_until")
    if locked_until and time.time() < locked_until:
        return int(locked_until - time.time())
    return 0


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        remaining = lockout_remaining()
        if remaining:
            return redirect(url_for("login"))

        if request.form.get("password") == APP_PASSWORD:
            session.pop("failed_attempts", None)
            session.pop("locked_until", None)
            session["auth"] = True
            return redirect(url_for("index"))
        else:
            now = time.time()
            attempts = [t for t in session.get("failed_attempts", []) if now - t < ATTEMPT_WINDOW]
            attempts.append(now)
            session["failed_attempts"] = attempts
            attempts_left = MAX_ATTEMPTS - len(attempts)
            if attempts_left <= 0:
                session["locked_until"] = now + LOCKOUT_SECONDS
                session.pop("failed_attempts", None)
            else:
                flash(f"Incorrect password. {attempts_left} attempt{'s' if attempts_left != 1 else ''} remaining.")
            return redirect(url_for("login"))

    remaining = lockout_remaining()
    if remaining:
        return render_template("login.html", locked=True, locked_seconds=remaining)

    error = get_flashed_messages()
    return render_template("login.html", locked=False, error=error[0] if error else None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not authenticated():
        return redirect(url_for("login"))
    return render_template("index.html")


@app.route("/api/scan/state")
def scan_state():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    remaining = cooldown_remaining()
    return jsonify({"active": remaining > 0, "remaining_ms": int(remaining * 1000)})


@app.route("/api/history")
def scan_history():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    def as_int(name, default):
        try:
            return max(0, int(request.args.get(name, default)))
        except (TypeError, ValueError):
            return default

    limit = min(as_int("limit", 50), MAX_ENTRIES)
    offset = as_int("offset", 0)
    try:
        entries = history.entries(limit=limit, offset=offset)
    except Exception:  # a broken history file must not break the page
        app.logger.exception("failed to read scan history")
        entries = []
    return jsonify({"entries": entries})


@app.route("/api/scan/progress")
def scan_progress():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        tasks = requests.get(f"{JELLYFIN_URL}/ScheduledTasks", headers=jf_headers(), timeout=10).json()
        scan_tasks = [
            t for t in tasks
            if any(kw in t.get("Name", "").lower() for kw in ("scan", "refresh", "media", "library"))
        ]
        running = next((t for t in scan_tasks if t.get("State") == "Running"), None)
        if running:
            pct = running.get("CurrentProgressPercentage") or 0
            return jsonify({"state": "running", "percent": round(pct, 1), "name": running.get("Name", "")})
        return jsonify({"state": "idle", "percent": 0})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def scan():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401

    with scan_lock:
        remaining = cooldown_remaining()
        if remaining > 0:
            record_press(OUTCOME_COOLDOWN)
            return jsonify({"error": "Scan cooldown active", "remaining_ms": int(remaining * 1000)}), 429

        if not JELLYFIN_URL or not JELLYFIN_API_KEY:
            msg = "JELLYFIN_URL or JELLYFIN_API_KEY not configured"
            record_press(OUTCOME_ERROR, error=msg)
            return jsonify({"error": msg}), 500

        try:
            r = requests.post(f"{JELLYFIN_URL}/Library/Refresh", headers=jf_headers(), timeout=10)
            r.raise_for_status()
        except Exception as e:
            record_press(OUTCOME_ERROR, error=str(e))
            return jsonify({"error": str(e)}), 500

        # Recorded only on success: this entry IS the cooldown.
        record_press(OUTCOME_STARTED)
        return jsonify({"status": "started"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
