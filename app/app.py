import json
import os
import time

import requests
from flask import Flask, Response, jsonify, render_template, stream_with_context

app = Flask(__name__)

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")


def jf_headers():
    return {"X-Emby-Token": JELLYFIN_API_KEY, "Accept": "application/json"}


def jf_get(path):
    r = requests.get(f"{JELLYFIN_URL}{path}", headers=jf_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def jf_post(path):
    r = requests.post(f"{JELLYFIN_URL}{path}", headers=jf_headers(), timeout=10)
    r.raise_for_status()
    return r


def jf_delete(path):
    r = requests.delete(f"{JELLYFIN_URL}{path}", headers=jf_headers(), timeout=10)
    r.raise_for_status()
    return r


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    if not JELLYFIN_URL or not JELLYFIN_API_KEY:
        return jsonify({"error": "JELLYFIN_URL or JELLYFIN_API_KEY not configured"}), 500
    try:
        info = jf_get("/System/Info")
        return jsonify({"ok": True, "server_name": info.get("ServerName", "Jellyfin"), "version": info.get("Version", "")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/libraries")
def libraries():
    try:
        data = jf_get("/Library/VirtualFolders")
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks")
def tasks():
    try:
        data = jf_get("/ScheduledTasks")
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan/all", methods=["POST"])
def scan_all():
    try:
        jf_post("/Library/Refresh")
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan/task/<task_id>", methods=["POST"])
def scan_task(task_id):
    try:
        jf_post(f"/ScheduledTasks/Running/{task_id}")
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scan/task/<task_id>/stop", methods=["POST"])
def stop_task(task_id):
    try:
        jf_delete(f"/ScheduledTasks/Running/{task_id}")
        return jsonify({"status": "stopped"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/progress")
def progress():
    def generate():
        while True:
            try:
                all_tasks = jf_get("/ScheduledTasks")
                scan_tasks = [
                    t for t in all_tasks
                    if any(kw in t.get("Name", "").lower() for kw in ("scan", "refresh", "media", "library"))
                ]
                yield f"data: {json.dumps({'tasks': scan_tasks})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
            time.sleep(2)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
