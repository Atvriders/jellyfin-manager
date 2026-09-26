import json
import logging
import os
import threading
import time

import pytest
import requests as real_requests

import app as app_module
import jellyfin
from conftest import FakeResponse, login, render_context, sign_in
from history import OUTCOME_BUSY, OUTCOME_COOLDOWN, OUTCOME_ERROR, OUTCOME_STARTED, ScanHistory

IDLE_TASKS = [
    # An unrelated task that happens to be running must be ignored: the
    # library scan is found by Key, never by name keywords.
    {"Name": "Refresh Guide", "Key": "RefreshGuide", "State": "Running", "CurrentProgressPercentage": 10},
    {"Name": "Scan Media Library", "Key": "RefreshLibrary", "State": "Idle"},
]


def library_tasks(state, pct=None, name="Scan Media Library"):
    task = {"Name": name, "Key": "RefreshLibrary", "State": state}
    if pct is not None:
        task["CurrentProgressPercentage"] = pct
    return [IDLE_TASKS[0], task]


def mock_tasks(monkeypatch, tasks=IDLE_TASKS, seen=None):
    def fake_get(url, **kwargs):
        if seen is not None:
            seen.append(url)
        assert url.endswith("/ScheduledTasks"), url
        return FakeResponse(payload=tasks)

    monkeypatch.setattr(app_module.requests, "get", fake_get)


def mock_refresh_ok(monkeypatch, seen=None, tasks=IDLE_TASKS):
    """Jellyfin with an idle library scan that accepts /Library/Refresh."""
    def fake_post(url, **kwargs):
        if seen is not None:
            seen.append(url)
        return FakeResponse(status_code=204)

    monkeypatch.setattr(app_module.requests, "post", fake_post)
    mock_tasks(monkeypatch, tasks)


# --- the global is gone ---------------------------------------------------


def test_scan_until_global_is_deleted():
    assert not hasattr(app_module, "scan_until")


# --- auth gating ----------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/history"),
        ("post", "/api/scan"),
        ("get", "/api/scan/state"),
        ("get", "/api/scan/progress"),
    ],
)
def test_endpoints_require_auth(client, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 401
    assert r.get_json()["error"] == "Unauthorized"


def test_unauthenticated_scan_records_nothing(client, hist_path):
    client.post("/api/scan")
    assert ScanHistory(hist_path).entries() == []


def test_index_redirects_to_login_when_anonymous(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_index_context_has_user_and_incoming_flag(client):
    with client.session_transaction() as sess:
        sign_in(sess, user="alice")
    status, seen = render_context(client, "/")
    assert status == 200
    ctx = seen["index.html"]
    assert ctx["user"] == "alice"
    assert ctx["incoming_enabled"] is False  # QBITTORRENT_INSTANCES unset


def test_index_user_defaults_to_empty(auth):
    _, seen = render_context(auth, "/")
    assert seen["index.html"]["user"] == ""


def test_login_flow_still_works(client, jf):
    jf.auth_status = 401
    bad = login(client)
    assert bad.status_code == 302

    with client.session_transaction() as sess:
        assert sess.get("auth") is not True

    jf.auth_status = 200
    ok = login(client)
    assert ok.status_code == 302
    with client.session_transaction() as sess:
        assert sess["auth"] is True

    assert client.get("/").status_code == 200


# --- POST /api/scan: success ----------------------------------------------


def test_scan_success_records_started_and_keeps_response_shape(auth, monkeypatch, hist_path):
    seen = []
    mock_refresh_ok(monkeypatch, seen)

    r = auth.post("/api/scan")

    assert r.status_code == 200
    assert r.get_json() == {"status": "started"}
    assert seen == ["http://jellyfin.test/Library/Refresh"]

    entries = ScanHistory(hist_path).entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == OUTCOME_STARTED
    assert entries[0]["error"] == ""


def test_scan_records_ip_and_user_agent(auth, monkeypatch, hist_path):
    mock_refresh_ok(monkeypatch)

    auth.post("/api/scan", headers={"User-Agent": "Mozilla/5.0 (TestBrowser)"})

    entry = ScanHistory(hist_path).entries()[0]
    assert entry["ip"] == "127.0.0.1"  # flask test client's remote_addr
    assert entry["user_agent"] == "Mozilla/5.0 (TestBrowser)"


def test_oversized_user_agent_header_is_truncated_end_to_end(auth, monkeypatch, hist_path):
    mock_refresh_ok(monkeypatch)

    auth.post("/api/scan", headers={"User-Agent": "A" * 50000})

    assert len(ScanHistory(hist_path).entries()[0]["user_agent"]) == 256


# --- POST /api/scan: cooldown ---------------------------------------------


def test_second_immediate_press_is_429_and_records_cooldown(auth, monkeypatch, hist_path):
    mock_refresh_ok(monkeypatch)

    first = auth.post("/api/scan")
    assert first.status_code == 200

    second = auth.post("/api/scan")
    assert second.status_code == 429
    body = second.get_json()
    assert body["error"] == "Scan cooldown active"
    assert 0 < body["remaining_ms"] <= app_module.COOLDOWN_SECONDS * 1000

    outcomes = [e["outcome"] for e in ScanHistory(hist_path).entries()]
    assert outcomes == [OUTCOME_COOLDOWN, OUTCOME_STARTED]  # newest first


def test_held_down_button_is_one_coalesced_cooldown_row(auth, monkeypatch, hist_path):
    """A press flood can't push the audit trail out of the 500-row cap."""
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200
    for _ in range(25):
        assert auth.post("/api/scan").status_code == 429

    rows = auth.get("/api/history").get_json()["entries"]
    assert [(r["outcome"], r["count"]) for r in rows] == [(OUTCOME_COOLDOWN, 25), (OUTCOME_STARTED, 1)]
    assert rows[0]["last_ts"] >= rows[0]["ts"]


def test_rejected_press_does_not_extend_the_cooldown(auth, monkeypatch):
    mock_refresh_ok(monkeypatch)
    auth.post("/api/scan")

    before = auth.get("/api/scan/state").get_json()["remaining_ms"]
    for _ in range(3):
        assert auth.post("/api/scan").status_code == 429
    after = auth.get("/api/scan/state").get_json()["remaining_ms"]

    assert after <= before  # strictly counting down, never reset


def test_cooldown_rejection_never_calls_jellyfin(auth, monkeypatch, hist_path):
    """The 2nd press must be rejected without touching the network."""
    calls = []
    mock_refresh_ok(monkeypatch, calls)
    auth.post("/api/scan")

    monkeypatch.setattr(app_module.requests, "post", lambda *a, **kw: calls.append("BOOM"))
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: calls.append("GET BOOM"))
    auth.post("/api/scan")

    assert calls == ["http://jellyfin.test/Library/Refresh"]


def test_expired_cooldown_allows_a_new_scan(auth, monkeypatch, hist_path):
    # An old "started" entry, 2 hours ago -> cooldown long expired.
    hist = ScanHistory(hist_path)
    hist.record(OUTCOME_STARTED)
    monkeypatch.setattr(app_module.history, "last_started_at", lambda: time.time() - 7200)
    mock_refresh_ok(monkeypatch)

    r = auth.post("/api/scan")
    assert r.status_code == 200
    assert r.get_json() == {"status": "started"}


# --- POST /api/scan: errors -----------------------------------------------


def test_simultaneous_presses_start_exactly_one_scan(client, monkeypatch, hist_path):
    """The check-then-fire must be atomic: the dev server is threaded, so two
    people (or two tabs) can press at the same instant. Exactly one scan wins;
    the loser is a normal 429."""
    fired = []

    def slow_post(url, **kwargs):
        fired.append(url)
        time.sleep(0.05)  # widen the window a real Jellyfin call would leave open
        return FakeResponse(status_code=204)

    monkeypatch.setattr(app_module.requests, "post", slow_post)
    mock_tasks(monkeypatch)

    codes = []

    def press():
        c = app_module.app.test_client()
        with c.session_transaction() as sess:
            sign_in(sess)
        codes.append(c.post("/api/scan").status_code)

    threads = [threading.Thread(target=press) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [200, 429, 429, 429, 429]
    assert len(fired) == 1  # Jellyfin was asked to refresh exactly once

    entries = ScanHistory(hist_path).entries()
    assert [e["outcome"] for e in entries].count(OUTCOME_STARTED) == 1
    # The four losers are the same user+ip inside a minute: they coalesce, so
    # count presses rather than rows.
    assert sum(e["count"] for e in entries if e["outcome"] == OUTCOME_COOLDOWN) == 4


def test_refresh_uses_the_shared_helper_auth_and_never_follows_redirects(auth, monkeypatch):
    seen = []

    def fake_post(url, **kwargs):
        seen.append((url, kwargs))
        return FakeResponse(status_code=204)

    monkeypatch.setattr(app_module.requests, "post", fake_post)
    mock_tasks(monkeypatch)
    assert auth.post("/api/scan").status_code == 200

    [(url, kwargs)] = seen
    assert url == "http://jellyfin.test/Library/Refresh"
    assert kwargs["headers"] == jellyfin.api_headers("api-key")  # MediaBrowser Token=
    assert "X-Emby-Token" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is False


def test_jellyfin_failure_records_error_and_returns_502(auth, monkeypatch, hist_path):
    mock_tasks(monkeypatch)
    monkeypatch.setattr(app_module.requests, "post", lambda url, **kw: FakeResponse(status_code=503))

    r = auth.post("/api/scan")
    assert r.status_code == 502
    assert r.get_json() == {"error": "Jellyfin returned HTTP 503"}

    entries = ScanHistory(hist_path).entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == OUTCOME_ERROR
    assert entries[0]["error"] == "Jellyfin returned HTTP 503"


def test_scan_error_never_leaks_the_internal_jellyfin_address(auth, monkeypatch, hist_path):
    """Raw requests exception text names the internal host:port; the page and
    the shared history get only the short, fixed-vocabulary reason."""
    mock_tasks(monkeypatch)

    def refused(url, **kwargs):
        raise real_requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='10.0.0.5', port=8096): Max retries exceeded with url: /Library/Refresh"
        )

    monkeypatch.setattr(app_module.requests, "post", refused)

    r = auth.post("/api/scan")
    assert r.status_code == 502
    assert r.get_json() == {"error": "Jellyfin unreachable"}
    body = r.get_data(as_text=True) + json.dumps(ScanHistory(hist_path).entries())
    for leak in ("10.0.0.5", "8096", "HTTPConnectionPool", "jellyfin.test"):
        assert leak not in body
    assert ScanHistory(hist_path).entries()[0]["error"] == "Jellyfin unreachable"


def test_redirecting_refresh_is_an_error_not_a_started_scan(auth, monkeypatch, hist_path):
    """A 3xx must not be followed (POST->GET whose 2xx looked like success)."""
    mock_tasks(monkeypatch)
    monkeypatch.setattr(app_module.requests, "post", lambda url, **kw: FakeResponse(status_code=302))
    r = auth.post("/api/scan")
    assert r.status_code == 502
    assert "redirected" in r.get_json()["error"]
    assert ScanHistory(hist_path).entries()[0]["outcome"] == OUTCOME_ERROR


def test_failed_scan_does_not_start_a_cooldown(auth, monkeypatch):
    mock_tasks(monkeypatch)

    def fake_post(url, **kwargs):
        raise real_requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(app_module.requests, "post", fake_post)

    assert auth.post("/api/scan").status_code == 502

    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is False
    assert state["remaining_ms"] == 0

    # ...so the user can immediately retry once Jellyfin is back.
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200


class CountInvalidations:
    def __init__(self):
        self.invalidated = 0

    def invalidate(self):
        self.invalidated += 1


@pytest.mark.parametrize("lost_answer", [
    real_requests.exceptions.ReadTimeout("read timed out"),
    real_requests.exceptions.ConnectionError(
        "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
    ),
])
def test_refresh_whose_answer_was_lost_counts_as_started(auth, monkeypatch, hist_path, lost_answer):
    """The Refresh left and Jellyfin most likely queued it; only the answer
    was lost. Treating that as a failure hands out a retry, and a second
    Refresh cancels the running scan and restarts it at 0% — so it is a start:
    the cooldown begins and the next press is a plain 429."""
    posted = []
    stub = CountInvalidations()
    monkeypatch.setattr(app_module, "incoming", stub)

    def lost(url, **kwargs):
        posted.append(url)
        raise lost_answer

    monkeypatch.setattr(app_module.requests, "post", lost)
    # The task list is as slow as the POST: the busy check can't save us.
    monkeypatch.setattr(app_module.requests, "get",
                        lambda url, **kw: (_ for _ in ()).throw(real_requests.exceptions.ReadTimeout("slow")))

    r = auth.post("/api/scan")
    assert r.status_code == 200
    assert r.get_json() == {"status": "started"}
    assert stub.invalidated == 1

    [entry] = ScanHistory(hist_path).entries()
    assert entry["outcome"] == OUTCOME_STARTED
    # The row says why it's uncertain, in the short safe vocabulary.
    assert entry["error"] == "Jellyfin was slow to answer; the scan was probably started"

    assert auth.get("/api/scan/state").get_json()["active"] is True
    assert auth.post("/api/scan").status_code == 429
    assert posted == ["http://jellyfin.test/Library/Refresh"]  # exactly one Refresh


def test_refresh_that_never_left_is_still_an_error(auth, monkeypatch, hist_path):
    """A connect timeout never reached Jellyfin: nothing started, so no
    cooldown — the user may retry as soon as Jellyfin is back."""
    mock_tasks(monkeypatch)

    def connect_timeout(url, **kwargs):
        raise real_requests.exceptions.ConnectTimeout("connect timed out")

    monkeypatch.setattr(app_module.requests, "post", connect_timeout)
    r = auth.post("/api/scan")
    assert r.status_code == 502
    assert r.get_json() == {"error": "Jellyfin timed out"}
    assert ScanHistory(hist_path).entries()[0]["outcome"] == OUTCOME_ERROR
    assert auth.get("/api/scan/state").get_json()["active"] is False


# --- POST /api/scan: a scan is already running ---------------------------------


@pytest.mark.parametrize("state", ["Running", "Cancelling"])
def test_busy_library_scan_is_409_and_never_posts_refresh(auth, monkeypatch, hist_path, state):
    """A second /Library/Refresh CANCELS the running scan and restarts it at
    0%. While one is running (or winding down) the press must not POST."""
    posted = []
    mock_refresh_ok(monkeypatch, posted, tasks=library_tasks(state, pct=37))

    r = auth.post("/api/scan")
    assert r.status_code == 409
    assert r.get_json() == {"error": "A library scan is already running.", "running": True}
    assert posted == []

    entries = ScanHistory(hist_path).entries()
    assert [e["outcome"] for e in entries] == [OUTCOME_BUSY]


def test_busy_press_does_not_start_the_cooldown(auth, monkeypatch):
    mock_refresh_ok(monkeypatch, tasks=library_tasks("Running", pct=5))
    assert auth.post("/api/scan").status_code == 409
    assert auth.get("/api/scan/state").get_json() == {"active": False, "remaining_ms": 0}

    # The scan finishes: the very next press is a normal start.
    posted = []
    mock_refresh_ok(monkeypatch, posted)
    assert auth.post("/api/scan").status_code == 200
    assert posted == ["http://jellyfin.test/Library/Refresh"]


def test_busy_presses_coalesce(auth, monkeypatch, hist_path):
    mock_refresh_ok(monkeypatch, tasks=library_tasks("Running"))
    for _ in range(5):
        assert auth.post("/api/scan").status_code == 409
    entries = ScanHistory(hist_path).entries()
    assert [(e["outcome"], e["count"]) for e in entries] == [(OUTCOME_BUSY, 5)]


def test_unrelated_running_task_does_not_block_the_scan(auth, monkeypatch):
    posted = []
    mock_refresh_ok(monkeypatch, posted)  # RefreshGuide is Running, RefreshLibrary Idle
    assert auth.post("/api/scan").status_code == 200
    assert posted == ["http://jellyfin.test/Library/Refresh"]


def test_task_lookup_failure_falls_through_to_the_refresh(auth, monkeypatch, hist_path):
    """Not knowing whether a scan runs is no reason to refuse one: the POST is
    the real test of whether Jellyfin is there."""
    posted = []
    mock_refresh_ok(monkeypatch, posted)

    def broken(url, **kwargs):
        raise real_requests.exceptions.Timeout("slow")

    monkeypatch.setattr(app_module.requests, "get", broken)
    assert auth.post("/api/scan").status_code == 200
    assert posted == ["http://jellyfin.test/Library/Refresh"]
    assert ScanHistory(hist_path).entries()[0]["outcome"] == OUTCOME_STARTED


def test_missing_library_task_is_not_busy(auth, monkeypatch):
    posted = []
    mock_refresh_ok(monkeypatch, posted, tasks=[IDLE_TASKS[0]])
    assert auth.post("/api/scan").status_code == 200
    assert posted == ["http://jellyfin.test/Library/Refresh"]


def test_missing_config_records_error_and_returns_500(auth, monkeypatch, hist_path):
    monkeypatch.setattr(app_module, "JELLYFIN_API_KEY", "")

    r = auth.post("/api/scan")
    assert r.status_code == 500
    assert "not configured" in r.get_json()["error"]

    entries = ScanHistory(hist_path).entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == OUTCOME_ERROR


def test_history_write_failure_does_not_break_the_scan_button(auth, monkeypatch):
    """An unwritable /data must not turn a working scan into an error."""
    mock_refresh_ok(monkeypatch)

    def explode(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(app_module.history, "record", explode)

    r = auth.post("/api/scan")
    assert r.status_code == 200
    assert r.get_json() == {"status": "started"}


def test_corrupt_history_file_does_not_break_the_scan_button(auth, monkeypatch, hist_path):
    with open(hist_path, "w") as f:
        f.write("not json at all {{{")

    mock_refresh_ok(monkeypatch)

    assert auth.get("/api/scan/state").status_code == 200
    r = auth.post("/api/scan")
    assert r.status_code == 200
    assert auth.get("/api/history").status_code == 200


# --- GET /api/scan/state (now derived from disk) --------------------------


def test_scan_state_idle_shape(auth):
    assert auth.get("/api/scan/state").get_json() == {"active": False, "remaining_ms": 0}


def test_scan_state_active_after_a_scan(auth, monkeypatch):
    mock_refresh_ok(monkeypatch)
    auth.post("/api/scan")

    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is True
    assert state["remaining_ms"] == pytest.approx(app_module.COOLDOWN_SECONDS * 1000, abs=5000)


def test_write_failure_keeps_the_cooldown_alive_in_process(auth, monkeypatch):
    """FINDING 4 regression: if the STARTED record can't be persisted (read-only
    /data, disk full), the button still works but the cooldown must NOT silently
    vanish -- an in-process floor keeps rate limiting alive for this process."""
    mock_refresh_ok(monkeypatch)

    def explode(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(app_module.history, "record", explode)

    # The scan itself still succeeds (a broken history file must not break it).
    assert auth.post("/api/scan").status_code == 200

    # last_started_at() reads the (empty, unwritten) file and returns 0, so
    # WITHOUT the in-process fallback the cooldown would be 0 and this second
    # press would be another 200 -- letting the button hammer Jellyfin.
    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is True
    assert state["remaining_ms"] > 0
    assert auth.post("/api/scan").status_code == 429


def test_cooldown_fails_closed_when_history_is_unreadable(auth, monkeypatch):
    """FINDING 5 regression: an OSError reading the history file (EMFILE/EACCES)
    must NOT be treated as 'no cooldown'. cooldown_remaining() fails CLOSED."""
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200  # start a real cooldown

    def boom():
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(app_module.history, "last_started_at", boom)

    # Fail closed: still active, not a free scan.
    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is True
    assert state["remaining_ms"] > 0
    assert auth.post("/api/scan").status_code == 429


def test_corrupt_file_cannot_hand_out_a_scan_the_process_knows_about(auth, monkeypatch, hist_path):
    """The file is quarantined as corrupt AFTER a scan: last_started_at() reads
    0.0, but this process saw that scan, so the cooldown still holds."""
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200

    with open(hist_path, "w") as f:
        f.write("}}} not json {{{")

    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is True
    assert state["remaining_ms"] > 0
    assert auth.post("/api/scan").status_code == 429


def test_future_started_row_is_ignored_and_warned_about(auth, monkeypatch, hist_path, caplog):
    """The clock jumped back two hours after a scan: that row can't freeze the
    button for three hours. It's ignored for the cooldown, and logged."""
    with open(hist_path, "w") as f:
        json.dump([{"id": "f", "ts": time.time() + 7200, "outcome": "started",
                    "ip": "", "user_agent": "", "error": "", "user": ""}], f)

    with caplog.at_level(logging.WARNING):
        state = auth.get("/api/scan/state").get_json()
    assert state == {"active": False, "remaining_ms": 0}
    assert any("future" in rec.getMessage() for rec in caplog.records)


def test_future_in_process_baselines_are_ignored_too(auth, monkeypatch):
    """The in-process cache/fallback can also predate a backwards jump."""
    monkeypatch.setattr(app_module, "_last_started_cache", time.time() + 7200)
    monkeypatch.setattr(app_module, "_last_started_fallback", time.time() + 7200)
    assert auth.get("/api/scan/state").get_json() == {"active": False, "remaining_ms": 0}


def test_remaining_never_exceeds_the_cooldown(auth, monkeypatch):
    monkeypatch.setattr(app_module.history, "last_started_at", lambda: time.time() + 30)  # within skew
    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is True
    assert state["remaining_ms"] <= app_module.COOLDOWN_SECONDS * 1000


def test_cooldown_survives_a_restart(auth, monkeypatch, hist_path):
    """THE regression this whole change exists to prevent."""
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200

    # Simulate a container restart: brand-new ScanHistory over the same file,
    # no in-memory state carried over.
    monkeypatch.setattr(app_module, "history", ScanHistory(hist_path))

    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is True
    assert state["remaining_ms"] > 0

    assert auth.post("/api/scan").status_code == 429  # still locked out


# --- GET /api/history -----------------------------------------------------


def test_history_returns_newest_first(auth, hist_path):
    app_module.history.record(OUTCOME_STARTED, ip="1.1.1.1")
    app_module.history.record(OUTCOME_COOLDOWN, ip="2.2.2.2")
    app_module.history.record(OUTCOME_ERROR, ip="3.3.3.3", error="nope")

    body = auth.get("/api/history").get_json()
    assert [e["ip"] for e in body["entries"]] == ["3.3.3.3", "2.2.2.2", "1.1.1.1"]
    assert [e["outcome"] for e in body["entries"]] == [OUTCOME_ERROR, OUTCOME_COOLDOWN, OUTCOME_STARTED]


def test_history_entry_shape(auth, monkeypatch):
    mock_refresh_ok(monkeypatch)
    auth.post("/api/scan", headers={"User-Agent": "UA/1"})

    entry = auth.get("/api/history").get_json()["entries"][0]
    assert set(entry) == {"id", "ts", "outcome", "ip", "user_agent", "error", "user", "count"}
    assert entry["count"] == 1


def test_history_empty(auth):
    assert auth.get("/api/history").get_json() == {"entries": [], "total": 0, "writable": True}


def test_history_total_is_all_stored_rows_not_the_page(auth):
    for i in range(10):
        app_module.history.record(OUTCOME_STARTED, ip=str(i))
    body = auth.get("/api/history?limit=3").get_json()
    assert len(body["entries"]) == 3
    assert body["total"] == 10


# "writable": whether presses are actually being saved. An unwritable /data
# used to be silent: every press "worked", nothing was kept, and the cooldown
# vanished on the next restart. The page warns when this is false.


def test_history_reports_writable_when_saving_works(auth, monkeypatch):
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200
    assert auth.get("/api/history").get_json()["writable"] is True


def test_a_failed_save_flags_history_unwritable_until_one_succeeds(auth, monkeypatch, hist_path):
    mock_refresh_ok(monkeypatch)
    real_record = app_module.history.record

    def explode(*a, **kw):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(app_module.history, "record", explode)
    assert auth.post("/api/scan").status_code == 200   # the scan itself still works
    assert auth.get("/api/history").get_json()["writable"] is False

    # /data comes back (remounted, chmod'ed): the next row that lands clears it.
    monkeypatch.setattr(app_module.history, "record", real_record)
    assert auth.post("/api/scan").status_code == 429    # the in-process cooldown held
    body = auth.get("/api/history").get_json()
    assert body["writable"] is True
    assert [e["outcome"] for e in body["entries"]] == [OUTCOME_COOLDOWN]


def _probe_errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR and "scan history" in r.getMessage()]


def _signed_in(mod):
    c = mod.app.test_client()
    with c.session_transaction() as sess:
        sign_in(sess)
    return c


def test_unwritable_data_dir_is_one_startup_error_and_unwritable_history(reload_app, tmp_path, monkeypatch, caplog):
    """A read-only /data must say so ONCE at startup — with the path, the uid
    the app runs as and why — and the page must be told."""
    import tempfile

    def read_only(*a, **kw):
        raise OSError(30, "Read-only file system")

    with monkeypatch.context() as m:
        m.setattr(tempfile, "mkstemp", read_only)
        with caplog.at_level(logging.WARNING):
            mod = reload_app(DATA_DIR=str(tmp_path))

    [error] = _probe_errors(caplog)
    message = error.getMessage()
    assert str(tmp_path / "scan_history.json") in message
    assert f"uid {os.getuid()}" in message
    assert "Read-only file system" in message
    assert _signed_in(mod).get("/api/history").get_json()["writable"] is False


def test_a_data_dir_that_is_a_file_does_not_crash_the_import(reload_app, tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    with caplog.at_level(logging.WARNING):
        mod = reload_app(DATA_DIR=str(blocker))
    [error] = _probe_errors(caplog)
    assert "not a directory" in error.getMessage()
    assert mod._history_writable is False
    assert mod.app.test_client().get("/healthz").status_code == 200


def test_single_file_bind_mount_is_reported_at_startup(reload_app, tmp_path, monkeypatch, caplog):
    history_file = str(tmp_path / "scan_history.json")
    real_ismount = os.path.ismount
    with monkeypatch.context() as m:
        m.setattr(os.path, "ismount", lambda p: os.path.abspath(p) == history_file or real_ismount(p))
        with caplog.at_level(logging.WARNING):
            mod = reload_app(DATA_DIR=str(tmp_path))

    [error] = _probe_errors(caplog)
    assert history_file in error.getMessage() and "mount" in error.getMessage()
    assert _signed_in(mod).get("/api/history").get_json()["writable"] is False


def test_writable_data_dir_starts_quietly_and_writable(reload_app, tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        mod = reload_app(DATA_DIR=str(tmp_path / "data"))
    assert _probe_errors(caplog) == []
    assert _signed_in(mod).get("/api/history").get_json()["writable"] is True
    assert os.listdir(tmp_path / "data") == []   # the probe cleaned up after itself


# A broken /data usually breaks READING the history too, so /api/history is a
# 503 — exactly when the page most needs to say "check the /data volume". The
# 503 carries "writable" as well, or the startup verdict could never reach it.


def test_a_data_dir_that_is_a_file_still_tells_the_page_history_isnt_saved(reload_app, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    r = _signed_in(reload_app(DATA_DIR=str(blocker))).get("/api/history")
    assert r.status_code == 503
    assert r.get_json() == {"error": "Couldn't read scan history", "writable": False}


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_data_dir_still_tells_the_page_history_isnt_saved(reload_app, tmp_path, caplog):
    """The wrong-owner / NFS root_squash case: mode 000 as far as we can tell."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            mod = reload_app(DATA_DIR=str(locked))
        [error] = _probe_errors(caplog)   # one, at startup
        r = _signed_in(mod).get("/api/history")
    finally:
        locked.chmod(0o755)
    assert "Permission denied" in error.getMessage()
    assert r.status_code == 503
    assert r.get_json()["writable"] is False


def test_a_history_path_that_is_a_directory_is_reported_at_startup(reload_app, tmp_path, caplog):
    """Docker creates a missing bind-mount source as a DIRECTORY. After an
    earlier single-file mount, switching to ./data:/data leaves
    data/scan_history.json/ behind: every read fails, the cooldown fails closed
    for good, and until now the startup check said all was well."""
    (tmp_path / "scan_history.json").mkdir()
    with caplog.at_level(logging.WARNING):
        mod = reload_app(DATA_DIR=str(tmp_path))
    [error] = _probe_errors(caplog)
    assert str(tmp_path / "scan_history.json") in error.getMessage()
    assert "is a directory" in error.getMessage()
    assert mod._history_writable is False
    r = _signed_in(mod).get("/api/history")
    assert r.status_code == 503
    assert r.get_json()["writable"] is False


def test_a_quarantined_history_is_logged_through_the_app_logger(reload_app, tmp_path, caplog):
    (tmp_path / "scan_history.json").write_text("}}} not json {{{")
    mod = reload_app(DATA_DIR=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        assert _signed_in(mod).get("/api/scan/state").status_code == 200
    [warning] = [r for r in caplog.records if "scan history" in r.getMessage() and r.levelno == logging.WARNING]
    assert warning.name == mod.app.logger.name
    assert str(tmp_path / "scan_history.json") in warning.getMessage()


def test_history_read_failure_is_503_not_an_empty_list(auth, monkeypatch):
    """An unreadable history must not look like "no presses yet"."""
    app_module.history.record(OUTCOME_STARTED)

    def boom(*a, **kw):
        raise OSError(5, "I/O error /data/scan_history.json")

    monkeypatch.setattr(app_module.history, "entries", boom)
    r = auth.get("/api/history")
    assert r.status_code == 503
    assert r.get_json() == {"error": "Couldn't read scan history", "writable": True}


def test_history_count_failure_is_503_too(auth, monkeypatch):
    def boom(*a, **kw):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(app_module.history, "count", boom)
    r = auth.get("/api/history")
    assert r.status_code == 503


def test_history_limit_and_offset(auth):
    for i in range(10):
        app_module.history.record(OUTCOME_STARTED, ip=str(i))

    page1 = auth.get("/api/history?limit=4").get_json()["entries"]
    page2 = auth.get("/api/history?limit=4&offset=4").get_json()["entries"]

    assert [e["ip"] for e in page1] == ["9", "8", "7", "6"]
    assert [e["ip"] for e in page2] == ["5", "4", "3", "2"]


def test_history_default_limit_is_50(auth):
    for i in range(60):
        app_module.history.record(OUTCOME_STARTED, ip=str(i))

    assert len(auth.get("/api/history").get_json()["entries"]) == 50


def test_history_garbage_query_params_do_not_500(auth):
    app_module.history.record(OUTCOME_STARTED)

    for qs in ("?limit=abc", "?offset=abc", "?limit=-5", "?offset=-5", "?limit=99999999"):
        r = auth.get("/api/history" + qs)
        assert r.status_code == 200, qs
        assert isinstance(r.get_json()["entries"], list)


# --- client IP / X-Forwarded-For ------------------------------------------


def test_x_forwarded_for_is_ignored_by_default(auth, monkeypatch, hist_path):
    """XFF is trivially spoofed; on a LAN we must not believe it."""
    mock_refresh_ok(monkeypatch)

    auth.post("/api/scan", headers={"X-Forwarded-For": "6.6.6.6"})

    assert ScanHistory(hist_path).entries()[0]["ip"] == "127.0.0.1"


def test_x_forwarded_for_is_used_when_trust_proxy_is_set(auth, monkeypatch, hist_path):
    monkeypatch.setenv("TRUST_PROXY", "1")
    mock_refresh_ok(monkeypatch)

    auth.post("/api/scan", headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})

    # Rightmost token = the address the single trusted proxy actually observed.
    assert ScanHistory(hist_path).entries()[0]["ip"] == "10.0.0.1"


def test_forged_leftmost_xff_cannot_poison_the_ip(auth, monkeypatch, hist_path):
    """FINDING 2 regression: a client that prepends a fake XFF entry must not be
    able to stamp a forged (or a victim's) IP into the history when behind a
    single trusted proxy. The proxy appends the true peer to the RIGHT."""
    monkeypatch.setenv("TRUST_PROXY", "1")
    mock_refresh_ok(monkeypatch)

    # Attacker sends "1.1.1.1"; the trusted proxy appends the real peer.
    auth.post("/api/scan", headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.50"})

    ip = ScanHistory(hist_path).entries()[0]["ip"]
    assert ip == "203.0.113.50"
    assert ip != "1.1.1.1"  # the attacker-controlled leftmost value is ignored


def test_trust_proxy_falls_back_to_remote_addr_when_no_xff(auth, monkeypatch, hist_path):
    monkeypatch.setenv("TRUST_PROXY", "1")
    mock_refresh_ok(monkeypatch)

    auth.post("/api/scan")

    assert ScanHistory(hist_path).entries()[0]["ip"] == "127.0.0.1"


def test_trust_proxy_0_means_no_proxy_and_is_not_a_mistake(auth, monkeypatch, hist_path, caplog):
    monkeypatch.setenv("TRUST_PROXY", "0")
    mock_refresh_ok(monkeypatch)

    with caplog.at_level(logging.WARNING):
        auth.post("/api/scan", headers={"X-Forwarded-For": "6.6.6.6"})

    assert ScanHistory(hist_path).entries()[0]["ip"] == "127.0.0.1"
    assert not [r for r in caplog.records if "TRUST_PROXY" in r.getMessage()]


def _ip_seen(xff=None):
    """client_ip() for one request (the Flask test client's peer is 127.0.0.1)."""
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    with app_module.app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": "127.0.0.1"}):
        return app_module.client_ip()


# TRUST_PROXY=<n>: n proxies in a row, each appending the address IT saw to the
# right. So the n-th entry from the right is what the outermost trusted proxy
# saw: the client. Everything left of it is whatever the client sent.


@pytest.mark.parametrize("hops,xff,expected", [
    ("1", "1.1.1.1, 203.0.113.50", "203.0.113.50"),
    ("2", "1.1.1.1, 203.0.113.50, 172.18.0.3", "203.0.113.50"),
    ("2", "203.0.113.50, 172.18.0.3", "203.0.113.50"),
    ("3", "6.6.6.6, 198.51.100.7, 10.0.0.9, 172.18.0.3", "198.51.100.7"),
    (" 2 ", "1.1.1.1, 203.0.113.50, 172.18.0.3", "203.0.113.50"),
])
def test_trust_proxy_n_takes_the_nth_entry_from_the_right(client, monkeypatch, hops, xff, expected):
    monkeypatch.setenv("TRUST_PROXY", hops)
    assert _ip_seen(xff) == expected


@pytest.mark.parametrize("hops,xff,expected", [
    # The client skipped the outer proxy (split DNS: LAN users go straight to
    # the inner one), or TRUST_PROXY counts one proxy too many. Either way the
    # rightmost entry is what the proxy next to the app saw; the connecting
    # address is that proxy, a private address that would pass for LAN.
    ("2", "203.0.113.50", "203.0.113.50"),
    ("3", "6.6.6.6, 203.0.113.50", "203.0.113.50"),
    ("3", "6.6.6.6 , 198.51.100.7 ", "198.51.100.7"),
    # No usable header at all: the connecting address is all there is.
    ("2", "", "127.0.0.1"),
    ("2", "  ", "127.0.0.1"),
    ("2", None, "127.0.0.1"),
])
def test_trust_proxy_n_with_fewer_entries_takes_the_rightmost_never_a_client_written_one(
        client, monkeypatch, hops, xff, expected):
    """Fewer entries than proxies means the chain isn't what the operator said
    it is. Everything left of the rightmost entry may be client-written."""
    monkeypatch.setenv("TRUST_PROXY", hops)
    assert _ip_seen(xff) == expected


@pytest.mark.parametrize("value", ["yes", "true", "-1", "1.5", "two", "1_0", "+2", "\u0662"])
def test_bad_trust_proxy_values_are_ignored_and_warned_about_once(client, monkeypatch, caplog, value):
    monkeypatch.setenv("TRUST_PROXY", value)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert _ip_seen("6.6.6.6, 203.0.113.50") == "127.0.0.1"
    warned = [r for r in caplog.records if "TRUST_PROXY" in r.getMessage()]
    assert len(warned) == 1
    assert warned[0].levelno == logging.WARNING
    assert repr(value) in warned[0].getMessage()


def test_each_distinct_bad_trust_proxy_value_is_warned_about(client, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        for value in ("yes", "yes", "no", "no"):
            monkeypatch.setenv("TRUST_PROXY", value)
            _ip_seen("203.0.113.50")
    assert len([r for r in caplog.records if "TRUST_PROXY" in r.getMessage()]) == 2


def test_a_bad_trust_proxy_value_is_reported_at_startup(reload_app, caplog):
    """Warned once when the app boots (where the operator looks), not first on
    some later request — and not again per request."""
    with caplog.at_level(logging.WARNING):
        mod = reload_app(TRUST_PROXY="true")
    warned = [r for r in caplog.records if "TRUST_PROXY" in r.getMessage()]
    assert len(warned) == 1
    with caplog.at_level(logging.WARNING):
        with mod.app.test_request_context("/", headers={"X-Forwarded-For": "203.0.113.50"},
                                          environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            assert mod.client_ip() == "127.0.0.1"
    assert len([r for r in caplog.records if "TRUST_PROXY" in r.getMessage()]) == 1


# --- GET /api/scan/progress ------------------------------------------------------
# The library scan is found by Key ("RefreshLibrary"), not by name keywords:
# names are localized, and "refresh"/"media" matched unrelated tasks.


def test_progress_reports_running_task(auth, monkeypatch):
    mock_tasks(monkeypatch, library_tasks("Running", pct=42.345))

    body = auth.get("/api/scan/progress").get_json()
    assert body == {"state": "running", "percent": 42.3, "name": "Scan Media Library"}


def test_progress_ignores_other_running_tasks(auth, monkeypatch):
    mock_tasks(monkeypatch, IDLE_TASKS)  # RefreshGuide Running at 10%
    assert auth.get("/api/scan/progress").get_json() == {"state": "idle", "percent": 0, "last": None}


def test_progress_uses_the_localized_name_or_a_default(auth, monkeypatch):
    mock_tasks(monkeypatch, library_tasks("Running", pct=1, name="Medienbibliothek scannen"))
    assert auth.get("/api/scan/progress").get_json()["name"] == "Medienbibliothek scannen"
    mock_tasks(monkeypatch, library_tasks("Running", pct=1, name=""))
    assert auth.get("/api/scan/progress").get_json()["name"] == "Scan Media Library"


def test_progress_cancelling_counts_as_running(auth, monkeypatch):
    mock_tasks(monkeypatch, library_tasks("Cancelling", pct=80))
    assert auth.get("/api/scan/progress").get_json()["state"] == "running"


@pytest.mark.parametrize("raw,expected", [
    (None, 0), ("junk", 0), (float("nan"), 0), (float("inf"), 0), (-5, 0), (250, 100), ("12.34", 12.3),
])
def test_progress_percent_is_always_a_sane_number(auth, monkeypatch, raw, expected):
    mock_tasks(monkeypatch, library_tasks("Running", pct=raw))
    r = auth.get("/api/scan/progress")
    json.loads(r.get_data(as_text=True))  # strict JSON: no NaN/Infinity tokens
    assert r.get_json()["percent"] == expected


def test_progress_missing_percent_is_zero(auth, monkeypatch):
    mock_tasks(monkeypatch, library_tasks("Running"))  # no CurrentProgressPercentage key
    assert auth.get("/api/scan/progress").get_json()["percent"] == 0


def test_progress_reports_idle(auth, monkeypatch):
    mock_tasks(monkeypatch, library_tasks("Idle", pct=0))

    assert auth.get("/api/scan/progress").get_json() == {"state": "idle", "percent": 0, "last": None}


# How the last scan ended. Without it the page could only say "Scan complete
# 100%" when a scan it was watching stopped — even one that was cancelled or
# failed half way.

STARTED_UTC, ENDED_UTC = "2026-09-26T14:00:00.0000000Z", "2026-09-26T14:03:11.1234567Z"
STARTED_EPOCH, ENDED_EPOCH = 1790431200.0, 1790431391.123456


def finished_library_task(status):
    [other, task] = library_tasks("Idle")
    task["LastExecutionResult"] = {"Status": status, "StartTimeUtc": STARTED_UTC, "EndTimeUtc": ENDED_UTC}
    return [other, task]


@pytest.mark.parametrize("status", ["Cancelled", "Failed", "Aborted", "Completed"])
def test_progress_idle_says_how_the_last_scan_ended(auth, monkeypatch, status):
    mock_tasks(monkeypatch, finished_library_task(status))
    body = auth.get("/api/scan/progress").get_json()
    assert body == {"state": "idle", "percent": 0,
                    "last": {"status": status, "started_at": STARTED_EPOCH, "ended_at": pytest.approx(ENDED_EPOCH)}}


def test_progress_idle_last_is_null_without_a_library_task(auth, monkeypatch):
    mock_tasks(monkeypatch, [IDLE_TASKS[0]])
    assert auth.get("/api/scan/progress").get_json() == {"state": "idle", "percent": 0, "last": None}


def test_progress_running_response_is_unchanged_by_the_last_result(auth, monkeypatch):
    [other, task] = finished_library_task("Failed")
    task.update(State="Running", CurrentProgressPercentage=12)
    mock_tasks(monkeypatch, [other, task])
    assert auth.get("/api/scan/progress").get_json() == {
        "state": "running", "percent": 12.0, "name": "Scan Media Library"}


# "last" is extra detail for an idle page. Before it existed the idle answer
# could not fail; a LastExecutionResult we can't read must not change that.


def test_progress_idle_survives_a_last_result_with_an_impossible_offset(auth, monkeypatch):
    [other, task] = finished_library_task("Failed")
    task["LastExecutionResult"]["StartTimeUtc"] = "2026-09-26T14:03:11+24:00"  # offsets stop short of 24h
    mock_tasks(monkeypatch, [other, task])
    r = auth.get("/api/scan/progress")
    assert r.status_code == 200
    body = r.get_json()
    assert (body["state"], body["percent"]) == ("idle", 0)
    assert body["last"] is None or body["last"]["status"] == "Failed"


def test_progress_idle_last_is_null_when_it_cant_be_read(auth, monkeypatch):
    mock_tasks(monkeypatch, finished_library_task("Failed"))

    def unreadable(task):
        raise ValueError("offset must be a timedelta strictly between -timedelta(hours=24) and ...")

    monkeypatch.setattr(jellyfin, "last_result", unreadable)
    r = auth.get("/api/scan/progress")
    assert r.status_code == 200
    assert r.get_json() == {"state": "idle", "percent": 0, "last": None}


def test_progress_errors_are_502_with_a_short_safe_reason(auth, monkeypatch):
    def fake_get(url, **kw):
        raise real_requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='10.0.0.5', port=8096): Max retries exceeded"
        )

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    r = auth.get("/api/scan/progress")
    assert r.status_code == 502
    assert r.get_json() == {"error": "Jellyfin unreachable"}
    text = r.get_data(as_text=True)
    for leak in ("10.0.0.5", "8096", "HTTPConnectionPool", "jellyfin.test", "http"):
        assert leak not in text


def test_progress_bad_api_key_is_502(auth, monkeypatch):
    monkeypatch.setattr(app_module.requests, "get", lambda url, **kw: FakeResponse(status_code=401))
    r = auth.get("/api/scan/progress")
    assert r.status_code == 502
    assert r.get_json() == {"error": "Jellyfin rejected the API key (HTTP 401)"}


def test_progress_does_not_pollute_history(auth, monkeypatch, hist_path):
    mock_tasks(monkeypatch, [])

    auth.get("/api/scan/progress")
    auth.get("/api/scan/state")

    assert ScanHistory(hist_path).entries() == []
