"""Jellyfin-account login: any valid Jellyfin user signs in with their own
username+password, verified via POST /Users/AuthenticateByName. APP_PASSWORD is
gone. Failed sign-ins are throttled SERVER-side (per IP and per username); the
old cookie-held lockout was bypassed by simply discarding the cookie. All
Jellyfin traffic is mocked via the `jf` fixture — no network ever."""

import glob
import os

import pytest
import requests as real_requests

import app as app_module
import jellyfin
from conftest import FakeResponse, flashes, login, render_context
from history import OUTCOME_STARTED, ScanHistory
from limiter import LoginLimiter

IP = "127.0.0.1"  # the Flask test client's remote_addr


def mock_refresh_ok(monkeypatch):
    """Jellyfin with an idle library scan that accepts /Library/Refresh."""
    monkeypatch.setattr(app_module.requests, "post", lambda url, **kw: FakeResponse(status_code=204))
    monkeypatch.setattr(
        app_module.requests, "get",
        lambda url, **kw: FakeResponse(payload=[{"Key": "RefreshLibrary", "State": "Idle"}]),
    )


def ip_failures():
    return app_module.login_limiter.ip_failures(IP)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# --- successful login -------------------------------------------------------


def test_successful_login_sets_auth_and_user(client, jf):
    r = login(client, username="alice", password="s3cret!")
    assert r.status_code == 302
    assert "/login" not in r.headers["Location"]  # redirected to index

    with client.session_transaction() as sess:
        assert sess["auth"] is True
        assert sess["user"] == "alice"
        assert sess["uid"] == "user-1"   # the Jellyfin user Id, for re-validation
        assert isinstance(sess["sid"], str) and len(sess["sid"]) >= 16  # revocable

    assert client.get("/").status_code == 200


def test_each_login_gets_a_fresh_random_sid(client, jf):
    login(client)
    with client.session_transaction() as sess:
        first = sess["sid"]
    login(client)
    with client.session_transaction() as sess:
        assert sess["sid"] != first


def test_session_user_is_the_server_canonical_name(client, jf):
    """The session stores the Name Jellyfin returns, not what was typed."""
    jf.user = {"Name": "Alice", "Id": "user-1"}
    login(client, username="aLiCe")
    with client.session_transaction() as sess:
        assert sess["user"] == "Alice"


def test_index_is_handed_the_signed_in_user(client, jf):
    """The page shows "Signed in as …" next to a sign-out form."""
    login(client)
    status, seen = render_context(client, "/")
    assert status == 200
    assert seen["index.html"]["user"] == "alice"


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
    """A signed-in session whose Jellyfin Name was empty still records a row."""
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
    # A redirect must never be followed: it would re-send the password to
    # wherever the 3xx points.
    assert kwargs["allow_redirects"] is False

    auth_header = kwargs["headers"]["Authorization"]
    assert auth_header == jellyfin.client_header()  # the shared helper, no Token
    assert auth_header.startswith("MediaBrowser ")
    assert 'Client="Jellyfin Manager"' in auth_header
    assert 'Device="jellyfin-manager"' in auth_header
    assert 'DeviceId="jellyfin-manager"' in auth_header
    assert f'Version="{jellyfin.CLIENT_VERSION}"' in auth_header
    assert "Token=" not in auth_header
    assert "X-Emby-Token" not in kwargs["headers"]

    # The password appears in the JSON body and NOWHERE else — including the
    # admin-key user lookup that precedes the sign-in.
    assert "hunter2-pw" not in url
    for value in kwargs["headers"].values():
        assert "hunter2-pw" not in value
    for get_url, get_kwargs in jf.gets:
        assert "hunter2-pw" not in get_url
        assert "hunter2-pw" not in repr(get_kwargs)


def test_successful_login_revokes_the_created_session(client, jf):
    jf.access_token = "tok-revoke-me"
    login(client)

    assert len(jf.logout_calls) == 1
    url, kwargs = jf.logout_calls[0]
    assert url == "http://jellyfin.test/Sessions/Logout"
    # The MediaBrowser header with the session's Token (newer Jellyfin ignores
    # the legacy X-Emby-Token header, so the revoke silently did nothing).
    assert kwargs["headers"] == {"Authorization": jellyfin.client_header("tok-revoke-me")}
    assert kwargs["allow_redirects"] is False


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


def test_mediabrowser_constant_is_gone():
    """The header comes from jellyfin.client_header() now; no second copy."""
    assert not hasattr(app_module, "MEDIABROWSER_AUTH_HEADER")
    assert not hasattr(app_module, "jf_headers")


# --- bad credentials: server-side attempt counting + lockout ------------------


def test_bad_credentials_consume_an_attempt_with_the_new_message(client, jf):
    jf.auth_status = 401
    login(client, password="wrong")

    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 1
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
    assert ip_failures() == 1


def test_unknown_user_is_the_generic_failure_without_calling_jellyfin_auth(client, jf):
    """No such Jellyfin user: same message as a wrong password (no account
    enumeration), consumes an attempt, and never reaches AuthenticateByName."""
    login(client, username="mallory")
    assert jf.auth_calls == []
    assert len(jf.user_list_calls) == 1
    assert ip_failures() == 1
    assert flashes(client) == ["Wrong username or password. 2 attempts remaining."]


def test_three_bad_attempts_in_window_lock_out_for_an_hour(client, jf):
    jf.auth_status = 401
    for _ in range(3):
        login(client)

    assert app_module.login_limiter.ip_locked(IP) == pytest.approx(3600, abs=5)

    # The locked page renders, and while locked NO Jellyfin call is made even
    # with correct credentials.
    status, seen = render_context(client)
    assert status == 200
    ctx = seen["login.html"]
    assert ctx["locked"] is True
    assert ctx["locked_seconds"] == pytest.approx(3600, abs=5)

    jf.auth_status = 200
    calls_before, gets_before = len(jf.calls), len(jf.gets)
    r = login(client)
    assert r.status_code == 302
    assert len(jf.calls) == calls_before and len(jf.gets) == gets_before
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True


def test_discarding_the_cookie_does_not_reset_the_lockout(client, jf):
    """THE regression: the old lockout lived in the session cookie, so a
    guesser just dropped the cookie and kept going (30/30 guesses reached
    Jellyfin in a repro). A brand-new client from the same IP stays locked."""
    jf.auth_status = 401
    guesses = 0
    for _ in range(30):
        fresh = app_module.app.test_client()  # no cookie at all
        before = len(jf.auth_calls)
        login(fresh, password=f"guess-{guesses}")
        guesses += len(jf.auth_calls) - before
    assert guesses == 3


def test_attempts_outside_the_window_expire(client, jf, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(app_module, "login_limiter", LoginLimiter(clock=clock))
    jf.auth_status = 401
    login(client)
    login(client)
    clock.now += 5 * 60 + 1  # age both attempts past the 5-minute window

    login(client)
    assert ip_failures() == 1  # old ones dropped, not locked
    assert app_module.login_limiter.ip_locked(IP) == 0


def test_successful_login_clears_failed_attempts(client, jf):
    jf.auth_status = 401
    login(client)
    jf.auth_status = 200
    login(client)
    with client.session_transaction() as sess:
        assert sess["auth"] is True
    assert ip_failures() == 0


def test_six_failures_for_one_username_refuse_it_from_any_ip(client, jf, monkeypatch):
    """Rotating IPs doesn't help a guesser: 6 failures for one username in
    15 minutes refuse that username everywhere — without calling Jellyfin."""
    monkeypatch.setenv("TRUST_PROXY", "1")
    jf.auth_status = 401
    for i in range(6):
        c = app_module.app.test_client()
        c.post("/login", data={"username": "alice", "password": "x"},
               headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert len(jf.auth_calls) == 6

    c = app_module.app.test_client()
    jf.auth_status = 200  # even the right password is refused for now
    c.post("/login", data={"username": "ALICE", "password": "s3cret!"},
           headers={"X-Forwarded-For": "198.51.100.77"})
    assert len(jf.auth_calls) == 6
    with c.session_transaction() as sess:
        assert sess.get("auth") is not True
        msgs = [m for _cat, m in sess.get("_flashes", [])]
    assert len(msgs) == 1 and msgs[0].startswith("Too many failed sign-ins for this username.")
    # The refusal consumed nothing from the new IP.
    assert app_module.login_limiter.ip_failures("198.51.100.77") == 0


# --- Jellyfin unreachable: distinct message, NO attempt consumed -------------


def test_connection_error_consumes_no_attempt(client, jf):
    jf.auth_exc = real_requests.exceptions.ConnectionError("no route to host")
    for _ in range(5):  # well past the lockout threshold
        login(client)

    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert app_module.login_limiter.ip_locked(IP) == 0
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."] * 5


def test_timeout_consumes_no_attempt(client, jf):
    jf.auth_exc = real_requests.exceptions.Timeout("timed out")
    login(client)
    assert ip_failures() == 0
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."]


def test_5xx_is_unreachable_not_a_failed_attempt(client, jf):
    jf.auth_status = 500
    login(client)
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."]


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirect_is_unreachable_not_a_credential_verdict(client, jf, status):
    jf.auth_status = status
    login(client)
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."]


def test_user_lookup_failure_is_unreachable_and_skips_the_sign_in(client, jf):
    jf.users_exc = real_requests.exceptions.ConnectionError("down")
    login(client)
    assert jf.auth_calls == []
    assert ip_failures() == 0
    assert flashes(client) == ["Can't reach the Jellyfin server. Try again in a moment."]


def test_unreachable_then_bad_creds_counts_only_the_real_rejections(client, jf):
    jf.auth_exc = real_requests.exceptions.ConnectionError("down")
    login(client)
    login(client)
    jf.auth_exc = None
    jf.auth_status = 401
    login(client)
    assert ip_failures() == 1
    assert app_module.login_limiter.ip_locked(IP) == 0


# --- missing fields / missing config: NO attempt consumed --------------------


@pytest.mark.parametrize(
    "form",
    [
        {},
        {"password": "pw"},
        {"username": "", "password": "pw"},
        {"username": "", "password": ""},
    ],
)
def test_missing_username_consumes_no_attempt_and_never_hits_jellyfin(client, jf, form):
    r = client.post("/login", data=form)
    assert r.status_code == 302
    assert jf.auth_calls == []
    assert jf.gets == []
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert flashes(client) == ["Enter your Jellyfin username and password."]


PASSWORDLESS = "Enter your password. Jellyfin accounts without a password can't sign in here."


@pytest.mark.parametrize("form", [{"username": "alice"}, {"username": "alice", "password": ""}])
def test_empty_password_gets_its_own_message_and_never_hits_jellyfin(client, jf, form):
    r = client.post("/login", data=form)
    assert r.status_code == 302
    assert jf.auth_calls == [] and jf.gets == []
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert flashes(client) == [PASSWORDLESS]


def test_a_passwordless_jellyfin_account_cannot_sign_in(client, jf):
    """Jellyfin signs a passwordless account in with an empty password. Such
    accounts are usually LAN-only, but Jellyfin only sees this container's
    address and, without TRUST_PROXY, so does our remote-access check for
    every tunnel visitor: nothing but this refusal stops a stranger who knows
    the name."""
    jf.users = [{"Name": "kids", "Id": "u-kids", "Policy": {"EnableRemoteAccess": False}}]
    jf.user = {"Name": "kids", "Id": "u-kids"}   # this mock says yes to anything
    for _ in range(5):
        client.post("/login", data={"username": "kids", "password": ""})
    assert jf.auth_calls == [] and jf.gets == []
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert app_module.login_limiter.user_locked("kids") == 0
    assert client.get("/").status_code == 302


def test_empty_password_keeps_the_typed_username(client, jf):
    client.post("/login", data={"username": "alice", "password": ""})
    _, seen = render_context(client)
    ctx = seen["login.html"]
    assert ctx["username"] == "alice"
    assert ctx["error"] == PASSWORDLESS


def test_unset_jellyfin_url_is_a_config_error_not_an_attempt(client, jf, monkeypatch):
    monkeypatch.setattr(app_module, "JELLYFIN_URL", "")
    login(client)
    assert jf.auth_calls == [] and jf.gets == []
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert ip_failures() == 0
    assert flashes(client) == ["JELLYFIN_URL is not configured."]


def test_unset_api_key_is_a_config_error_not_an_attempt(client, jf, monkeypatch):
    """The account guard needs the API key to read the user's Policy."""
    monkeypatch.setattr(app_module, "JELLYFIN_API_KEY", "")
    login(client)
    assert jf.auth_calls == [] and jf.gets == []
    assert ip_failures() == 0
    assert flashes(client) == ["JELLYFIN_API_KEY is not configured."]


# --- the login page ------------------------------------------------------------


def test_login_page_context_when_not_locked(client):
    status, seen = render_context(client)
    assert status == 200
    ctx = seen["login.html"]
    assert ctx["locked"] is False
    assert ctx["error"] is None
    assert ctx["username"] == ""


def test_failed_login_prefills_the_username_but_never_the_password(client, jf):
    jf.auth_status = 401
    login(client, username="alice", password="hunter2-pw")

    status, seen = render_context(client)
    ctx = seen["login.html"]
    assert ctx["username"] == "alice"
    assert ctx["error"] == "Wrong username or password. 2 attempts remaining."
    assert "hunter2-pw" not in repr(ctx)
    with client.session_transaction() as sess:
        assert "hunter2-pw" not in repr(dict(sess))

    # Shown once, right after the failure — not forever.
    _, seen = render_context(client)
    assert seen["login.html"]["username"] == ""


def test_prefilled_username_is_clipped(client, jf):
    jf.auth_status = 401
    login(client, username="u" * 10_000)
    _, seen = render_context(client)
    assert len(seen["login.html"]["username"]) <= 128


# --- session hygiene ----------------------------------------------------------


def test_prelogin_session_values_do_not_survive_login(client, jf):
    """session.clear() on success: prevents fixation and drops stale state."""
    with client.session_transaction() as sess:
        sess["sentinel"] = "planted-before-login"
        sess["sid"] = "attacker-chosen-sid"

    login(client)

    with client.session_transaction() as sess:
        assert sess["auth"] is True
        assert sess["user"] == "alice"
        assert "sentinel" not in sess
        assert sess["sid"] != "attacker-chosen-sid"


def test_logout_clears_the_user(client, jf):
    login(client)
    r = client.post("/logout")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
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
