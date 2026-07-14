"""Jellyfin-account login: any valid Jellyfin user signs in with their own
username+password, verified via POST /Users/AuthenticateByName. APP_PASSWORD is
gone. All Jellyfin traffic is mocked via the `jf` fixture — no network ever."""

import glob
import os
import time

import pytest
import requests as real_requests

import app as app_module
from conftest import FakeResponse, flashes, login
from history import OUTCOME_STARTED, ScanHistory


def mock_refresh_ok(monkeypatch):
    monkeypatch.setattr(
        app_module.requests, "post", lambda url, headers=None, timeout=None: FakeResponse()
    )


# --- successful login -------------------------------------------------------


def test_successful_login_sets_auth_and_user(client, jf):
    r = login(client, username="alice", password="s3cret!")
    assert r.status_code == 302
    assert "/login" not in r.headers["Location"]  # redirected to index

    with client.session_transaction() as sess:
        assert sess["auth"] is True
        assert sess["user"] == "alice"

    assert client.get("/").status_code == 200


def test_session_user_is_the_server_canonical_name(client, jf):
    """The session stores the Name Jellyfin returns, not what was typed."""
    jf.user = {"Name": "Alice", "Id": "user-1"}
    login(client, username="aLiCe")
    with client.session_transaction() as sess:
        assert sess["user"] == "Alice"


def test_scan_records_the_logged_in_user(client, jf, monkeypatch, hist_path):
    login(client, username="alice")
    mock_refresh_ok(monkeypatch)

    assert client.post("/api/scan").status_code == 200

    entries = ScanHistory(hist_path).entries()
    assert entries[0]["outcome"] == OUTCOME_STARTED
    assert entries[0]["user"] == "alice"


def test_api_history_rows_carry_the_user(client, jf, monkeypatch):
    login(client, username="alice")
    mock_refresh_ok(monkeypatch)
    client.post("/api/scan")
    client.post("/api/scan")  # cooldown rejection — still stamped with the user

    entries = client.get("/api/history").get_json()["entries"]
    assert len(entries) == 2
    assert all(e["user"] == "alice" for e in entries)


def test_scan_without_session_user_records_empty_user(auth, monkeypatch, hist_path):
    """Sessions from before this feature have auth but no user."""
    mock_refresh_ok(monkeypatch)
    auth.post("/api/scan")
    assert ScanHistory(hist_path).entries()[0]["user"] == ""


# --- what leaves the app: header, body, token revoke ------------------------


def test_mediabrowser_header_sent_and_password_only_in_body(client, jf):
    login(client, username="alice", password="hunter2-pw")

    assert len(jf.auth_calls) == 1
    url, kwargs = jf.auth_calls[0]
    assert url == "http://jellyfin.test/Users/AuthenticateByName"
    assert kwargs["json"] == {"Username": "alice", "Pw": "hunter2-pw"}
    assert kwargs["timeout"] == 10

    auth_header = kwargs["headers"]["Authorization"]
    assert auth_header.startswith("MediaBrowser ")
    assert 'Client="Jellyfin Manager"' in auth_header
    assert 'Device="jellyfin-manager"' in auth_header
    assert 'DeviceId="jellyfin-manager"' in auth_header
    assert 'Version="2.0.0"' in auth_header

    # The password appears in the JSON body and NOWHERE else.
    assert "hunter2-pw" not in url
    for value in kwargs["headers"].values():
        assert "hunter2-pw" not in value


def test_successful_login_revokes_the_created_session(client, jf):
    jf.access_token = "tok-revoke-me"
    login(client)

    assert len(jf.logout_calls) == 1
    url, kwargs = jf.logout_calls[0]
    assert url == "http://jellyfin.test/Sessions/Logout"
    assert kwargs["headers"] == {"X-Emby-Token": "tok-revoke-me"}


def test_revoke_failure_does_not_break_login(client, jf):
    jf.logout_exc = real_requests.exceptions.ConnectionError("logout boom")
    r = login(client)
    assert r.status_code == 302
    with client.session_transaction() as sess:
        assert sess["auth"] is True
        assert sess["user"] == "alice"


def test_bad_credentials_never_trigger_a_logout_call(client, jf):
    jf.auth_status = 401
    login(client)
    assert jf.logout_calls == []


# --- bad credentials: attempt counting + lockout -----------------------------


def test_bad_credentials_consume_an_attempt_with_the_new_message(client, jf):
    jf.auth_status = 401
    login(client, password="wrong")

    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
        assert len(sess["failed_attempts"]) == 1
    assert flashes(client) == ["Wrong username or password. 2 attempts remaining."]


def test_second_bad_attempt_message_is_singular(client, jf):
    jf.auth_status = 401
    login(client)
    with client.session_transaction() as sess:
        sess.pop("_flashes", None)
    login(client)
    assert flashes(client) == ["Wrong username or password. 1 attempt remaining."]


def test_400_is_also_a_credential_rejection(client, jf):
    jf.auth_status = 400
    login(client)
    with client.session_transaction() as sess:
        assert len(sess["failed_attempts"]) == 1


def test_three_bad_attempts_in_window_lock_out_for_an_hour(client, jf):
    jf.auth_status = 401
    for _ in range(3):
        login(client)

    with client.session_transaction() as sess:
        locked_until = sess["locked_until"]
        assert "failed_attempts" not in sess
    assert locked_until == pytest.approx(time.time() + app_module.LOCKOUT_SECONDS, abs=5)

    # The locked page renders, and while locked NO Jellyfin call is made even
    # with correct credentials.
    assert client.get("/login").status_code == 200
    jf.auth_status = 200
    calls_before = len(jf.auth_calls)
    r = login(client)
    assert r.status_code == 302
    assert len(jf.auth_calls) == calls_before
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True


def test_attempts_outside_the_window_expire(client, jf):
    jf.auth_status = 401
    login(client)
    with client.session_transaction() as sess:
        # Age the recorded attempt past the 5-minute window.
        sess["failed_attempts"] = [time.time() - app_module.ATTEMPT_WINDOW - 1]

    login(client)
    with client.session_transaction() as sess:
        assert len(sess["failed_attempts"]) == 1  # old one dropped, not locked
        assert "locked_until" not in sess


def test_successful_login_clears_failed_attempts(client, jf):
    jf.auth_status = 401
    login(client)
    jf.auth_status = 200
    login(client)
    with client.session_transaction() as sess:
        assert sess["auth"] is True
        assert "failed_attempts" not in sess


# --- Jellyfin unreachable: distinct message, NO attempt consumed -------------


def test_connection_error_consumes_no_attempt(client, jf):
    jf.auth_exc = real_requests.exceptions.ConnectionError("no route to host")
    for _ in range(5):  # well past the lockout threshold
        login(client)

    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
        assert "failed_attempts" not in sess
        assert "locked_until" not in sess
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."] * 5


def test_timeout_consumes_no_attempt(client, jf):
    jf.auth_exc = real_requests.exceptions.Timeout("timed out")
    login(client)
    with client.session_transaction() as sess:
        assert "failed_attempts" not in sess
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."]


def test_5xx_is_unreachable_not_a_failed_attempt(client, jf):
    jf.auth_status = 500
    login(client)
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
        assert "failed_attempts" not in sess
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."]


def test_unreachable_then_bad_creds_counts_only_the_real_rejections(client, jf):
    jf.auth_exc = real_requests.exceptions.ConnectionError("down")
    login(client)
    login(client)
    jf.auth_exc = None
    jf.auth_status = 401
    login(client)
    with client.session_transaction() as sess:
        assert len(sess["failed_attempts"]) == 1
        assert "locked_until" not in sess


# --- missing fields / missing config: NO attempt consumed --------------------


@pytest.mark.parametrize(
    "form",
    [
        {},
        {"username": "alice"},
        {"password": "pw"},
        {"username": "", "password": "pw"},
        {"username": "alice", "password": ""},
    ],
)
def test_missing_fields_consume_no_attempt_and_never_hit_jellyfin(client, jf, form):
    r = client.post("/login", data=form)
    assert r.status_code == 302
    assert jf.auth_calls == []
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
        assert "failed_attempts" not in sess
    assert flashes(client) == ["Enter your Jellyfin username and password."]


def test_unset_jellyfin_url_is_a_config_error_not_an_attempt(client, jf, monkeypatch):
    monkeypatch.setattr(app_module, "JELLYFIN_URL", "")
    login(client)
    assert jf.auth_calls == []
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
        assert "failed_attempts" not in sess
    assert flashes(client) == ["JELLYFIN_URL is not configured."]


# --- session hygiene ----------------------------------------------------------


def test_prelogin_session_values_do_not_survive_login(client, jf):
    """session.clear() on success: prevents fixation and drops attempt state."""
    with client.session_transaction() as sess:
        sess["sentinel"] = "planted-before-login"
        sess["failed_attempts"] = [time.time()]

    login(client)

    with client.session_transaction() as sess:
        assert sess["auth"] is True
        assert sess["user"] == "alice"
        assert "sentinel" not in sess
        assert "failed_attempts" not in sess


def test_logout_clears_the_user(client, jf):
    login(client)
    client.get("/logout")
    with client.session_transaction() as sess:
        assert "auth" not in sess
        assert "user" not in sess


# --- APP_PASSWORD is gone everywhere -----------------------------------------


def test_app_password_is_gone_from_the_module():
    assert not hasattr(app_module, "APP_PASSWORD")


def test_no_app_password_in_any_app_source_file():
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    checked = []
    for py in glob.glob(os.path.join(app_dir, "*.py")):
        checked.append(py)
        with open(py, encoding="utf-8") as f:
            assert "APP_PASSWORD" not in f.read(), py
    assert checked  # the glob actually found app.py & history.py
