import os
import time

import requests
from flask import Flask, flash, get_flashed_messages, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 60 * 60  # 1 hour


def jf_headers():
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"}


def authenticated():
    return session.get("auth") is True


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
            failed = session.get("failed_attempts", 0) + 1
            session["failed_attempts"] = failed
            attempts_left = MAX_ATTEMPTS - failed
            if attempts_left <= 0:
                session["locked_until"] = time.time() + LOCKOUT_SECONDS
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
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return jsonify({"error": "JELLYFIN_URL or JELLYFIN_API_KEY not configured"}), 500
    try:
        r = requests.post(f"{JELLYFIN_URL}/Library/Refresh", headers=jf_headers(), timeout=10)
        r.raise_for_status()
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
