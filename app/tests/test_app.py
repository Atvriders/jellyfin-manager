import threading
import time

import pytest

import app as app_module
from history import OUTCOME_COOLDOWN, OUTCOME_ERROR, OUTCOME_STARTED, ScanHistory


class FakeResponse:
    def __init__(self, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        return self._payload


@pytest.fixture
def hist_path(tmp_path):
    return str(tmp_path / "scan_history.json")


@pytest.fixture
def client(hist_path, monkeypatch):
    """A configured, isolated app. No network, no /data, no shared globals."""
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    # Reset the in-process cooldown safety-net globals so tests don't leak the
    # fallback/cache into one another.
    monkeypatch.setattr(app_module, "_last_started_fallback", 0.0)
    monkeypatch.setattr(app_module, "_last_started_cache", 0.0)
    monkeypatch.setattr(app_module, "history", ScanHistory(hist_path))
    monkeypatch.setattr(app_module, "JELLYFIN_URL", "http://jellyfin.test")
    monkeypatch.setattr(app_module, "JELLYFIN_API_KEY", "api-key")
    monkeypatch.setattr(app_module, "APP_PASSWORD", "hunter2")
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app_module.app.secret_key = "test-secret"

    def boom(*a, **kw):  # any unmocked network call is a test bug
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(app_module.requests, "post", boom)
    monkeypatch.setattr(app_module.requests, "get", boom)

    return app_module.app.test_client()


@pytest.fixture
def auth(client):
    with client.session_transaction() as sess:
        sess["auth"] = True
    return client


def mock_refresh_ok(monkeypatch, seen=None):
    def fake_post(url, headers=None, timeout=None):
        if seen is not None:
            seen.append(url)
        return FakeResponse()

    monkeypatch.setattr(app_module.requests, "post", fake_post)


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


def test_login_flow_still_works(client):
    bad = client.post("/login", data={"password": "wrong"})
    assert bad.status_code == 302

    with client.session_transaction() as sess:
        assert sess.get("auth") is not True

    ok = client.post("/login", data={"password": "hunter2"})
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

    def slow_post(url, headers=None, timeout=None):
        fired.append(url)
        time.sleep(0.05)  # widen the window a real Jellyfin call would leave open
        return FakeResponse()

    monkeypatch.setattr(app_module.requests, "post", slow_post)

    codes = []

    def press():
        c = app_module.app.test_client()
        with c.session_transaction() as sess:
            sess["auth"] = True
        codes.append(c.post("/api/scan").status_code)

    threads = [threading.Thread(target=press) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [200, 429, 429, 429, 429]
    assert len(fired) == 1  # Jellyfin was asked to refresh exactly once

    outcomes = [e["outcome"] for e in ScanHistory(hist_path).entries()]
    assert outcomes.count(OUTCOME_STARTED) == 1
    assert outcomes.count(OUTCOME_COOLDOWN) == 4


def test_jellyfin_failure_records_error_and_returns_500(auth, monkeypatch, hist_path):
    def fake_post(url, headers=None, timeout=None):
        return FakeResponse(exc=RuntimeError("503 Server Error"))

    monkeypatch.setattr(app_module.requests, "post", fake_post)

    r = auth.post("/api/scan")
    assert r.status_code == 500
    assert r.get_json()["error"] == "503 Server Error"

    entries = ScanHistory(hist_path).entries()
    assert len(entries) == 1
    assert entries[0]["outcome"] == OUTCOME_ERROR
    assert "503" in entries[0]["error"]


def test_failed_scan_does_not_start_a_cooldown(auth, monkeypatch):
    def fake_post(url, headers=None, timeout=None):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module.requests, "post", fake_post)

    assert auth.post("/api/scan").status_code == 500

    state = auth.get("/api/scan/state").get_json()
    assert state["active"] is False
    assert state["remaining_ms"] == 0

    # ...so the user can immediately retry once Jellyfin is back.
    mock_refresh_ok(monkeypatch)
    assert auth.post("/api/scan").status_code == 200


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
    assert set(entry) == {"id", "ts", "outcome", "ip", "user_agent", "error"}


def test_history_empty(auth):
    assert auth.get("/api/history").get_json() == {"entries": []}


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


def test_trust_proxy_only_honours_exactly_1(auth, monkeypatch, hist_path):
    monkeypatch.setenv("TRUST_PROXY", "0")
    mock_refresh_ok(monkeypatch)

    auth.post("/api/scan", headers={"X-Forwarded-For": "6.6.6.6"})

    assert ScanHistory(hist_path).entries()[0]["ip"] == "127.0.0.1"


# --- GET /api/scan/progress (must stay untouched) -------------------------


def test_progress_reports_running_task(auth, monkeypatch):
    tasks = [
        {"Name": "Some Other Task", "State": "Running", "CurrentProgressPercentage": 10},
        {"Name": "Scan Media Library", "State": "Running", "CurrentProgressPercentage": 42.345},
    ]
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse(payload=tasks))

    body = auth.get("/api/scan/progress").get_json()
    assert body == {"state": "running", "percent": 42.3, "name": "Scan Media Library"}


def test_progress_reports_idle(auth, monkeypatch):
    tasks = [{"Name": "Scan Media Library", "State": "Idle", "CurrentProgressPercentage": 0}]
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse(payload=tasks))

    assert auth.get("/api/scan/progress").get_json() == {"state": "idle", "percent": 0}


def test_progress_errors_are_500(auth, monkeypatch):
    def fake_get(*a, **kw):
        raise RuntimeError("jellyfin unreachable")

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    r = auth.get("/api/scan/progress")
    assert r.status_code == 500
    assert r.get_json()["error"] == "jellyfin unreachable"


def test_progress_does_not_pollute_history(auth, monkeypatch, hist_path):
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse(payload=[]))

    auth.get("/api/scan/progress")
    auth.get("/api/scan/state")

    assert ScanHistory(hist_path).entries() == []
