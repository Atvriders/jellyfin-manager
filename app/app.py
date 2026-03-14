import os

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")


def jf_headers():
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scan", methods=["POST"])
def scan():
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
