import os

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


def jf_headers():
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"}


def authenticated():
    return session.get("auth") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["auth"] = True
            return redirect(url_for("index"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    if not authenticated():
        return redirect(url_for("login"))
    return render_template("index.html")


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
