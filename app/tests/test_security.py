"""Security hardening: the server-side login limiter, SECRET_KEY handling,
cookie flags, security headers, /healthz, logout revocation, session
re-validation, and the Jellyfin account-disable guard.

All Jellyfin traffic is mocked (the `jf` fixture) — no network ever.
"""

import pytest

from limiter import LoginLimiter, PasswordCheckTimer, PendingSignIns


class Clock:
    """An injectable clock the tests move by hand."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def limiter(clock):
    return LoginLimiter(clock=clock)


# ================================================================== LoginLimiter


def test_limiter_defaults_match_the_old_cookie_numbers():
    lim = LoginLimiter()
    assert (lim.ip.limit, lim.ip.window, lim.ip.lockout) == (3, 5 * 60, 60 * 60)
    assert (lim.user.limit, lim.user.window, lim.user.lockout) == (6, 15 * 60, 15 * 60)


def test_three_ip_failures_lock_that_ip_for_an_hour(limiter, clock):
    assert limiter.failed("203.0.113.5", "alice") == 2
    assert limiter.failed("203.0.113.5", "bob") == 1
    assert limiter.ip_locked("203.0.113.5") == 0
    assert limiter.failed("203.0.113.5", "carol") == 0  # 0 left: now locked
    assert limiter.ip_locked("203.0.113.5") == 3600

    clock.advance(3599)
    assert limiter.ip_locked("203.0.113.5") == 1
    clock.advance(1)
    assert limiter.ip_locked("203.0.113.5") == 0


def test_ip_lock_is_per_ip(limiter):
    for _ in range(3):
        limiter.failed("203.0.113.5", "alice")
    assert limiter.ip_locked("203.0.113.5") > 0
    assert limiter.ip_locked("203.0.113.6") == 0


def test_ip_failures_outside_the_window_expire(limiter, clock):
    limiter.failed("203.0.113.5", "a")
    limiter.failed("203.0.113.5", "b")
    clock.advance(5 * 60 + 1)
    # The two old failures aged out: this is failure 1 of 3 again.
    assert limiter.failed("203.0.113.5", "c") == 2
    assert limiter.ip_locked("203.0.113.5") == 0


def test_lock_starts_a_fresh_streak_when_it_expires(limiter, clock):
    for _ in range(3):
        limiter.failed("203.0.113.5", "alice")
    clock.advance(3600)
    assert limiter.ip_locked("203.0.113.5") == 0
    assert limiter.failed("203.0.113.5", "alice") == 2  # not instantly re-locked


def test_ip_failures_counts_the_current_window(limiter, clock):
    assert limiter.ip_failures("203.0.113.5") == 0
    limiter.failed("203.0.113.5", "a")
    limiter.failed("203.0.113.5", "b")
    assert limiter.ip_failures("203.0.113.5") == 2
    clock.advance(5 * 60)
    assert limiter.ip_failures("203.0.113.5") == 0
    for _ in range(3):
        limiter.failed("203.0.113.5", "a")
    assert limiter.ip_failures("203.0.113.5") == 0  # locked: the streak was consumed


def test_success_forgives_the_ips_failures_against_that_account(limiter):
    """The real owner mistyping, then getting it right, starts clean — the
    way clearing the session used to. Case-insensitive, like the account."""
    limiter.failed("203.0.113.5", "alice")
    limiter.failed("203.0.113.5", "alice")
    limiter.succeeded("203.0.113.5", "Alice")
    assert limiter.ip_failures("203.0.113.5") == 0
    assert limiter.failed("203.0.113.5", "alice") == 2


def test_success_as_one_account_does_not_forgive_failures_against_another(limiter):
    """Otherwise anyone with an account resets their IP's count at will:
    2 guesses at other people's passwords, sign in as yourself, repeat — and
    the 3-strike IP lock never fires (8 of 8 guesses got through in a repro)."""
    limiter.failed("203.0.113.5", "alice")
    limiter.failed("203.0.113.5", "bob")
    limiter.succeeded("203.0.113.5", "mallory")
    assert limiter.ip_failures("203.0.113.5") == 2
    assert limiter.failed("203.0.113.5", "carol") == 0  # third strike: locked
    assert limiter.ip_locked("203.0.113.5") == 3600


def test_success_forgives_only_its_own_share_of_a_mixed_streak(limiter):
    limiter.failed("203.0.113.5", "bob")
    limiter.failed("203.0.113.5", "mallory")  # a typo of her own
    limiter.succeeded("203.0.113.5", "mallory")
    assert limiter.ip_failures("203.0.113.5") == 1  # bob's stays
    assert limiter.failed("203.0.113.5", "carol") == 1


def test_ipv6_clients_are_counted_per_64(limiter):
    """One IPv6 client holds at least a /64 and can pick any address in it;
    counting single addresses would give it 2^64 fresh sets of attempts."""
    assert limiter.failed("2001:db8:5a3c:8e10::1", "a") == 2
    assert limiter.failed("2001:db8:5a3c:8e10::2", "b") == 1
    assert limiter.failed("2001:db8:5a3c:8e10:dead:beef:0:3", "c") == 0
    assert limiter.ip_locked("2001:db8:5a3c:8e10:ffff::9") == 3600
    # The neighbouring /64 belongs to someone else.
    assert limiter.ip_locked("2001:db8:5a3c:8e11::1") == 0


def test_ipv4_mapped_addresses_share_the_ipv4_counter(limiter):
    """A dual-stack socket reports IPv4 clients as ::ffff:a.b.c.d."""
    limiter.failed("::ffff:203.0.113.5", "a")
    limiter.failed("::FFFF:203.0.113.5", "b")
    assert limiter.failed("203.0.113.5", "c") == 0
    assert limiter.ip_locked("::ffff:203.0.113.5") > 0


@pytest.mark.parametrize("ip", ["", "not-an-ip", "fe80::1%eth0", " 203.0.113.5 ", "x" * 1000])
def test_odd_ip_strings_are_keyed_without_crashing(limiter, ip):
    for _ in range(3):
        limiter.failed(ip, "a")
    assert limiter.ip_locked(ip) > 0
    assert max(len(k) for k in limiter.ip.keys()) <= 128


def test_username_lock_spans_ips_and_is_case_insensitive(limiter, clock):
    """6 failures for one username from ANY mix of IPs refuse that username
    for 15 minutes — the cookie lockout was per-browser and this is not."""
    names = ["alice", "ALICE", "Alice", "aLiCe", "alice", "ALIce"]
    for i, name in enumerate(names):
        assert limiter.user_locked("alice") == 0
        limiter.failed(f"198.51.100.{i}", name)  # 6 different IPs, none locked
    assert limiter.user_locked("Alice") == 900
    assert limiter.user_locked("bob") == 0
    for i in range(6):
        assert limiter.ip_locked(f"198.51.100.{i}") == 0

    clock.advance(900)
    assert limiter.user_locked("alice") == 0


def test_username_failures_outside_15_minutes_expire(limiter, clock):
    for i in range(5):
        limiter.failed(f"198.51.100.{i}", "alice")
    clock.advance(15 * 60 + 1)
    limiter.failed("198.51.100.9", "alice")
    assert limiter.user_locked("alice") == 0


def test_success_does_not_forgive_the_username_streak(limiter):
    """A distributed guesser must not be reset by the real owner signing in."""
    for i in range(5):
        limiter.failed(f"198.51.100.{i}", "alice")
    limiter.succeeded("198.51.100.200", "alice")
    limiter.failed("198.51.100.9", "alice")
    assert limiter.user_locked("alice") > 0


def test_locked_seconds_round_up_never_down_to_zero(limiter, clock):
    for _ in range(3):
        limiter.failed("203.0.113.5", "a")
    clock.advance(3599.5)
    assert limiter.ip_locked("203.0.113.5") == 1  # 0.5 s left is still locked


def test_expired_entries_are_pruned(limiter, clock):
    """Memory must not grow with every IP/username that ever failed once."""
    for i in range(50):
        limiter.failed(f"198.51.100.{i}", f"user{i}")
    assert limiter.tracked() > 0
    clock.advance(60 * 60 + 61)
    limiter.failed("203.0.113.99", "zed")  # any call may prune
    # Only the fresh failure is still tracked (one IP + one username).
    assert limiter.tracked() == 2


def test_key_count_is_hard_capped(clock):
    lim = LoginLimiter(clock=clock, max_keys=10)
    for i in range(100):
        lim.failed(f"198.51.100.{i}", f"user{i}")
    assert lim.tracked() <= 2 * 2 * 10  # (failures + locks) x (ip + user)


def test_absurd_usernames_are_clipped_for_the_key(limiter):
    limiter.failed("203.0.113.5", "x" * 100_000)
    assert max(len(k) for k in limiter.user.keys()) <= 128


def test_limiter_is_thread_safe(clock):
    import threading

    lim = LoginLimiter(clock=clock, ip_limit=10_000, user_limit=10_000)
    barrier = threading.Barrier(8)

    def hammer(n):
        barrier.wait()
        for _ in range(200):
            lim.failed("203.0.113.5", f"u{n}")

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 1600 failures landed; none were lost to a race.
    assert lim.failed("203.0.113.5", "u0") == 10_000 - 1601


def test_user_failed_counts_toward_the_username_streak_only(limiter):
    """For a sign-in whose answer never arrived: it probably counted in
    Jellyfin, so it counts against the account — but it told the client
    nothing, so it costs the IP nothing."""
    for _ in range(5):
        limiter.user_failed("alice")
    assert limiter.user_locked("alice") == 0
    limiter.user_failed("ALICE")
    assert limiter.user_locked("alice") == 15 * 60
    assert limiter.ip_failures("127.0.0.1") == 0


# ------------------------------------------------------------------ PendingSignIns


def test_pending_sign_ins_count_until_they_expire(clock):
    pending = PendingSignIns(hold=120, clock=clock)
    assert pending.count("u-1") == 0
    pending.add("u-1")
    clock.advance(60)
    pending.add("u-1")
    assert (pending.count("u-1"), pending.count("u-2")) == (2, 0)
    clock.advance(60)
    assert pending.count("u-1") == 1          # the first one's hold is over
    clock.advance(60)
    assert pending.count("u-1") == 0
    assert len(pending) == 0                  # pruned, not kept forever


# ------------------------------------------------------------------ PasswordCheckTimer


def test_timer_pads_to_a_recently_measured_password_check(clock):
    slept = []
    timer = PasswordCheckTimer(default=0.3, clock=clock, sleep=slept.append)
    timer.pad(started=clock())                 # nothing measured yet: the default
    assert slept == [pytest.approx(0.3)]
    for seconds in (0.21, 0.25, 0.24):
        timer.record(seconds)
    slept.clear()
    started = clock()
    clock.advance(0.05)                        # time already spent counts
    timer.pad(started)
    assert len(slept) == 1 and 0.21 - 0.05 - 1e-9 <= slept[0] <= 0.25 - 0.05 + 1e-9


def test_timer_never_sleeps_negative_or_absurdly_long(clock):
    slept = []
    timer = PasswordCheckTimer(default=0.3, clock=clock, sleep=slept.append)
    started = clock()
    clock.advance(5)
    timer.pad(started)                         # already slower than a check
    assert slept == []
    timer.record(60.0)                         # one pathological sample
    timer.pad(clock())
    assert slept and slept[0] <= PasswordCheckTimer.MAX_PAD


def test_timer_ignores_junk_samples(clock):
    timer = PasswordCheckTimer(default=0.3, clock=clock, sleep=lambda s: None)
    for junk in (-1, float("nan"), float("inf")):
        timer.record(junk)
    assert timer.samples() == []


# ================================================================== the app

import logging  # noqa: E402
import time  # noqa: E402
from datetime import timedelta  # noqa: E402

import requests as real_requests  # noqa: E402

import app as app_module  # noqa: E402
from conftest import JellyfinAuthMock, default_policy, flashes, login, sign_in  # noqa: E402

IP = "127.0.0.1"
PASSWORD = "s3cret!"
# A genuinely global address for "a client on the internet". (The RFC 5737
# documentation ranges would work too now that "local" is an explicit LAN list,
# but a real public address says what it means.)
PUBLIC_IP = "8.8.8.8"

# Every sign-in that fails for a reason tied to the account (no such user,
# disabled, LAN-only, one failure from Jellyfin's lockout, ...) reads exactly
# like a wrong password, so the page can't be used to find out which accounts
# exist or what state they're in. The reason goes to the log.
WRONG = "Wrong username or password. 2 attempts remaining."
UNREACHABLE = "Can't reach the Jellyfin server. Try again in a moment."

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)


# ------------------------------------------------------------------ headers


@pytest.mark.parametrize("method,path,status", [
    ("get", "/login", 200),
    ("get", "/", 302),
    ("get", "/api/scan/state", 401),
    ("post", "/api/scan", 401),
    ("get", "/healthz", 200),
    ("get", "/static/jellyfield.js", 200),
    ("get", "/no-such-page", 404),
    ("get", "/logout", 405),
])
def test_security_headers_on_every_response(client, method, path, status):
    r = getattr(client, method)(path)
    assert r.status_code == status
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert r.headers["Content-Security-Policy"] == CSP


def test_security_headers_on_an_authenticated_page(auth):
    r = auth.get("/")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == CSP
    assert r.headers["X-Frame-Options"] == "DENY"


# ------------------------------------------------------------------ /healthz


def test_healthz_is_ok_plain_text_without_auth(client):
    """Docker's HEALTHCHECK: no session, no Jellyfin call (the client fixture
    fails any network call), nothing but "ok"."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.mimetype == "text/plain"
    assert r.get_data(as_text=True) == "ok"
    assert "Set-Cookie" not in r.headers


# ------------------------------------------------------------------ SECRET_KEY


PLACEHOLDERS = [
    "change_this_to_a_random_string",       # the shipped docker-compose value
    "some_long_random_string",              # README's example, copied verbatim
    "a_Random_String_of_my_own_choosing",
    "CHANGE_THIS_please_0123456789abcdef",
    "changeme-changeme-changeme-123",
    "ChangeMe_but_long_enough_to_pass",
    "your_secret_key_goes_here_12345",
]


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_secret_key_is_random_with_a_warning(raw, caplog):
    log = logging.getLogger("test-secret")
    with caplog.at_level(logging.WARNING, logger="test-secret"):
        key = app_module.resolve_secret_key(raw, log)
    assert isinstance(key, bytes) and len(key) >= 32
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert "sessions will not survive a restart" in caplog.records[0].getMessage()


@pytest.mark.parametrize("raw", PLACEHOLDERS + ["tiny-key-9", "fifteen-chars-!"])
def test_placeholder_or_short_secret_key_is_replaced_with_an_error(raw, caplog):
    """A published placeholder lets anyone forge {"auth": true}. Refuse it —
    but don't crash: deployments auto-pull the image and would just go down."""
    log = logging.getLogger("test-secret")
    with caplog.at_level(logging.WARNING, logger="test-secret"):
        key = app_module.resolve_secret_key(raw, log)
    assert key != raw
    assert isinstance(key, bytes) and len(key) >= 32
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    # The rejected value itself is never written to the log.
    assert raw not in caplog.text


def test_good_secret_key_is_used_as_is_and_silently(caplog):
    good = "k7Qz3v9pX2mB8nL4tR6wY1cF5hJ0sD"
    with caplog.at_level(logging.WARNING, logger="test-secret"):
        assert app_module.resolve_secret_key(good, logging.getLogger("test-secret")) == good
    assert caplog.records == []


def test_random_keys_differ():
    log = logging.getLogger("test-secret")
    assert app_module.resolve_secret_key("", log) != app_module.resolve_secret_key("", log)


def _jellyfin_on_requests(monkeypatch):
    mock = JellyfinAuthMock()
    monkeypatch.setattr(real_requests, "post", mock.post)
    monkeypatch.setattr(real_requests, "get", mock.get)
    return mock


def test_placeholder_secret_key_is_never_the_signing_key(reload_app, monkeypatch, caplog):
    placeholder = "change_this_to_a_random_string"
    with caplog.at_level(logging.ERROR):
        mod = reload_app(SECRET_KEY=placeholder)
    assert mod.app.secret_key != placeholder
    assert any(r.levelno == logging.ERROR and "SECRET_KEY" in r.getMessage() for r in caplog.records)

    # ...and the app still works: a real sign-in succeeds.
    _jellyfin_on_requests(monkeypatch)
    c = mod.app.test_client()
    r = c.post("/login", data={"username": "alice", "password": PASSWORD})
    assert r.status_code == 302 and "/login" not in r.headers["Location"]
    assert c.get("/").status_code == 200


@pytest.mark.parametrize("placeholder", [
    "change_this_to_a_random_string",  # docker-compose.yml / .env.example
    "some_long_random_string",         # README.md's setup example
])
def test_a_session_forged_with_the_placeholder_is_rejected(reload_app, monkeypatch, placeholder):
    """The actual attack: sign {"auth": true, ...} with a published key."""
    from flask.sessions import SecureCookieSessionInterface

    mod = reload_app(SECRET_KEY=placeholder)
    _jellyfin_on_requests(monkeypatch)

    import flask
    forger = flask.Flask("forger")
    forger.secret_key = placeholder
    forged = SecureCookieSessionInterface().get_signing_serializer(forger).dumps(
        {"auth": True, "uid": "user-1", "sid": "x", "recheck_at": time.time() + 600}
    )
    c = mod.app.test_client()
    c.set_cookie("session", forged)
    assert c.get("/api/scan/state").status_code == 401


def test_good_secret_key_from_env_is_used(reload_app):
    mod = reload_app(SECRET_KEY="k7Qz3v9pX2mB8nL4tR6wY1cF5hJ0sD")
    assert mod.app.secret_key == "k7Qz3v9pX2mB8nL4tR6wY1cF5hJ0sD"


# ------------------------------------------------------------------ cookie flags


def test_session_cookie_flags_default(client, jf):
    r = login(client)
    cookie = next(v for v in r.headers.getlist("Set-Cookie") if v.startswith("session="))
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    # Off by default: the LAN plain-HTTP port must keep working.
    assert "Secure" not in cookie
    assert app_module.app.permanent_session_lifetime == timedelta(days=7)


def test_secure_cookies_opt_in(reload_app, monkeypatch):
    mod = reload_app(SECRET_KEY="k7Qz3v9pX2mB8nL4tR6wY1cF5hJ0sD", SECURE_COOKIES="1")
    assert mod.app.config["SESSION_COOKIE_SECURE"] is True
    _jellyfin_on_requests(monkeypatch)
    r = mod.app.test_client().post("/login", data={"username": "alice", "password": PASSWORD})
    cookie = next(v for v in r.headers.getlist("Set-Cookie") if v.startswith("session="))
    assert "Secure" in cookie


@pytest.mark.parametrize("value", ["0", "true", "yes", ""])
def test_secure_cookies_only_honours_exactly_1(reload_app, value):
    mod = reload_app(SECRET_KEY="k7Qz3v9pX2mB8nL4tR6wY1cF5hJ0sD", SECURE_COOKIES=value)
    assert mod.app.config["SESSION_COOKIE_SECURE"] is False


# ------------------------------------------------------------------ logout


def test_logout_is_post_only(client, jf):
    """A GET logout can be fired cross-site by an <img>; POST can't carry the
    SameSite=Lax cookie cross-site."""
    login(client)
    r = client.get("/logout")
    assert r.status_code == 405
    with client.session_transaction() as sess:
        assert sess["auth"] is True  # still signed in


def test_a_copied_cookie_is_dead_after_logout(client, jf):
    login(client)
    stolen = client.get_cookie("session").value
    assert client.get("/api/scan/state").status_code == 200

    assert client.post("/logout").status_code == 302

    client.set_cookie("session", stolen)  # replay the pre-logout cookie
    assert client.get("/api/scan/state").status_code == 401
    assert client.get("/").status_code == 302


def test_a_revoked_cookie_is_never_re_signed(client, jf):
    """Any request that would re-save a revoked session (e.g. a login POST that
    stores the typed username) must not hand back a fresh signature on it —
    that would outlive the revocation list."""
    login(client)
    stolen = client.get_cookie("session").value
    client.post("/logout")

    client.set_cookie("session", stolen)
    client.post("/login", data={"username": "alice"})  # missing password: no auth
    with client.session_transaction() as sess:
        assert "sid" not in sess and "auth" not in sess


def test_revocations_are_kept_for_the_session_lifetime_then_pruned(client, jf, monkeypatch):
    login(client)
    with client.session_transaction() as sess:
        sid = sess["sid"]
    client.post("/logout")
    until = app_module._revoked_sids[sid]
    assert until == pytest.approx(time.time() + 7 * 24 * 3600, abs=5)

    # Once no copy of that cookie can still verify, the entry is dropped.
    app_module._revoked_sids[sid] = time.time() - 1
    login(client)
    client.post("/logout")
    assert sid not in app_module._revoked_sids


def test_authenticated_itself_rejects_a_revoked_sid(client):
    """Defence in depth: the before-request hook normally drops a revoked
    session first, but authenticated() must not rely on that hook running."""
    from flask import session

    app_module.revoke_sid("gone")
    with app_module.app.test_request_context("/"):
        sign_in(session, sid="gone")
        assert app_module.authenticated() is False
        assert "auth" not in session


def test_logout_other_session_is_unaffected(client, jf):
    other = app_module.app.test_client()
    login(other)
    login(client)
    client.post("/logout")
    assert other.get("/api/scan/state").status_code == 200


# ------------------------------------------------------------------ sessions from before this upgrade


@pytest.mark.parametrize("sess_values", [
    {"auth": True},
    {"auth": True, "user": "alice"},
    {"auth": True, "sid": "s"},          # no uid
    {"auth": True, "uid": "user-1"},     # no sid
    {"auth": True, "uid": "", "sid": "s"},
    {"auth": "yes", "uid": "user-1", "sid": "s"},
])
def test_sessions_without_uid_and_sid_are_logged_out(client, sess_values):
    with client.session_transaction() as sess:
        sess.update(sess_values)
    assert client.get("/api/scan/state").status_code == 401
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True


# ------------------------------------------------------------------ re-validation against Jellyfin


def _due(client, **extra):
    with client.session_transaction() as sess:
        sign_in(sess, user="alice")
        sess["recheck_at"] = time.time() - 1
        sess.update(extra)


def test_revalidation_is_skipped_while_fresh(auth, jf):
    assert auth.get("/api/scan/state").status_code == 200
    assert jf.user_get_calls == []


def test_revalidation_when_due_keeps_a_healthy_session(client, jf):
    _due(client)
    assert client.get("/api/scan/state").status_code == 200
    [(url, kwargs)] = jf.user_get_calls
    assert url == "http://jellyfin.test/Users/user-1"
    assert kwargs["headers"]["Authorization"].endswith('Token="api-key"')
    with client.session_transaction() as sess:
        assert sess["recheck_at"] == pytest.approx(time.time() + 600, abs=5)
    # ...and not again for the next ten minutes.
    client.get("/api/scan/state")
    assert len(jf.user_get_calls) == 1


def test_deleted_jellyfin_user_is_signed_out(client, jf, caplog):
    jf.users = []  # GET /Users/user-1 -> 404
    _due(client)
    with caplog.at_level(logging.WARNING):
        assert client.get("/api/scan/state").status_code == 401
    with client.session_transaction() as sess:
        assert "auth" not in sess
    assert any("alice" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_disabled_jellyfin_user_is_signed_out(client, jf):
    jf.set_policy(IsDisabled=True)
    _due(client)
    assert client.get("/api/scan/state").status_code == 401
    assert client.get("/").status_code == 302


def test_remote_access_revoked_signs_out_a_remote_session(client, jf, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    jf.set_policy(EnableRemoteAccess=False)
    _due(client)
    r = client.get("/api/scan/state", headers={"X-Forwarded-For": PUBLIC_IP})
    assert r.status_code == 401


def test_remote_access_revoked_keeps_a_lan_session(client, jf):
    jf.set_policy(EnableRemoteAccess=False)
    _due(client)
    assert client.get("/api/scan/state").status_code == 200  # 127.0.0.1


@pytest.mark.parametrize("failure", ["exc", "status"])
def test_jellyfin_outage_does_not_sign_anyone_out(client, jf, failure):
    if failure == "exc":
        jf.users_exc = real_requests.exceptions.ConnectionError("down")
    else:
        jf.users_status = 503
    _due(client)
    assert client.get("/api/scan/state").status_code == 200
    with client.session_transaction() as sess:
        assert sess["auth"] is True
        # Retried soon — not on every request (each would wait on Jellyfin).
        assert sess["recheck_at"] == pytest.approx(time.time() + 60, abs=5)


def test_recheck_time_far_in_the_future_forces_a_recheck(client, jf):
    """A clock that jumped backwards must not postpone re-validation for hours."""
    _due(client, recheck_at=time.time() + 30 * 24 * 3600)
    client.get("/api/scan/state")
    assert len(jf.user_get_calls) == 1


# ------------------------------------------------------------------ account-disable guard
# Jellyfin DISABLES an account once Policy.InvalidLoginAttemptCount reaches
# Policy.LoginAttemptsBeforeLockout, and only a successful login resets the
# counter. This public page must never be the thing that trips it.


def _refused_without_calling(client, jf):
    """Refused before the password went anywhere — and, to the client,
    indistinguishable from a wrong password (message and attempt cost)."""
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert jf.auth_calls == []
    assert app_module.login_limiter.ip_failures(IP) == 1
    assert flashes(client) == [WRONG]


def test_disabled_account_is_refused_before_authenticating(client, jf):
    jf.set_policy(IsDisabled=True)
    login(client)
    _refused_without_calling(client, jf)


@pytest.mark.parametrize("limit,count", [(3, 2), (3, 7), (5, 4), (10, 9)])
def test_one_more_failure_would_disable_it_so_refuse(client, jf, limit, count):
    jf.set_policy(LoginAttemptsBeforeLockout=limit, InvalidLoginAttemptCount=count)
    login(client)
    _refused_without_calling(client, jf)


@pytest.mark.parametrize("limit,count", [(3, 1), (5, 3), (10, 0), (-1, 999), (None, 999)])
def test_accounts_with_headroom_or_no_lockout_proceed(client, jf, limit, count):
    policy = {"InvalidLoginAttemptCount": count}
    if limit is not None:
        policy["LoginAttemptsBeforeLockout"] = limit
    jf.users[0]["Policy"] = policy
    r = login(client)
    assert len(jf.auth_calls) == 1
    assert "/login" not in r.headers["Location"]


@pytest.mark.parametrize("limit", [0, 1])
def test_accounts_that_lock_on_the_first_failure_are_refused(client, jf, limit):
    """Jellyfin disables when count+1 >= limit. With limit 1 that's the very
    first failure; a stored 0 (legacy DBs — the policy editor maps 0 to 3 only
    on save, and the DTO reports the stored value) disables on the first
    failure too. "Reset it by signing in" can't help, so say what's true."""
    jf.set_policy(LoginAttemptsBeforeLockout=limit, InvalidLoginAttemptCount=0)
    login(client)
    _refused_without_calling(client, jf)


def test_missing_policy_proceeds(client, jf):
    del jf.users[0]["Policy"]
    login(client)
    assert len(jf.auth_calls) == 1


@pytest.mark.parametrize("junk", [{"IsDisabled": "yes"}, {"LoginAttemptsBeforeLockout": "3", "InvalidLoginAttemptCount": "9"},
                                  {"LoginAttemptsBeforeLockout": True}, "not-a-dict"])
def test_junk_policy_values_are_ignored(client, jf, junk):
    jf.users[0]["Policy"] = junk
    login(client)
    assert len(jf.auth_calls) == 1


def test_remote_access_off_is_refused_from_outside_the_lan(client, jf, monkeypatch):
    """Jellyfin only sees this container's LAN address, so it would let a
    LAN-only user in from anywhere. Refuse on its behalf."""
    monkeypatch.setenv("TRUST_PROXY", "1")
    jf.set_policy(EnableRemoteAccess=False)
    client.post("/login", data={"username": "alice", "password": PASSWORD},
                headers={"X-Forwarded-For": PUBLIC_IP})
    assert jf.auth_calls == []
    assert app_module.login_limiter.ip_failures(PUBLIC_IP) == 1
    assert flashes(client) == [WRONG]


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.77.20",
                                "169.254.10.1", "::1", "fd00::5", "fe80::1", "::ffff:192.168.77.5"])
def test_remote_access_off_still_allows_lan_clients(client, jf, monkeypatch, ip):
    monkeypatch.setenv("TRUST_PROXY", "1")
    jf.set_policy(EnableRemoteAccess=False)
    client.post("/login", data={"username": "alice", "password": PASSWORD},
                headers={"X-Forwarded-For": ip})
    assert len(jf.auth_calls) == 1


@pytest.mark.parametrize("ip", [
    PUBLIC_IP, "1.1.1.1", "100.64.0.1", "2001:4860::8888", "not-an-ip", "",
    # Internet clients that Python's is_private still calls "private":
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo (2001::/23)
    "2002:808:808::1",                       # 6to4 (2002::/16)
    "::ffff:8.8.8.8",                        # a public IPv4 client on a dual-stack socket
    "192.0.2.10",                            # not a LAN range either
])
def test_remote_access_off_refuses_public_or_unparseable_addresses(client, jf, monkeypatch, ip):
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.setattr(app_module, "client_ip", lambda: ip)
    jf.set_policy(EnableRemoteAccess=False)
    login(client)
    assert jf.auth_calls == []


def test_remote_access_on_allows_public_clients(client, jf, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    client.post("/login", data={"username": "alice", "password": PASSWORD},
                headers={"X-Forwarded-For": PUBLIC_IP})
    assert len(jf.auth_calls) == 1


# TRUST_PROXY=<n> (n proxies in a row): the sign-in throttle and the
# remote-access check key on the SAME address — the n-th X-Forwarded-For entry
# from the right — never on the client-written leftmost one.


def test_two_proxies_throttle_the_client_address(client, jf, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "2")
    jf.auth_status = 401
    client.post("/login", data={"username": "alice", "password": "guess"},
                headers={"X-Forwarded-For": "192.168.77.5, 8.8.4.4, 172.18.0.3"})
    assert len(jf.auth_calls) == 1
    assert app_module.login_limiter.ip_failures("8.8.4.4") == 1
    for not_the_client in ("192.168.77.5", "172.18.0.3", IP):
        assert app_module.login_limiter.ip_failures(not_the_client) == 0


def test_two_proxies_remote_access_check_uses_the_client_address(client, jf, monkeypatch):
    """A remote client can't pass as LAN by writing a LAN address on the left;
    the proxy next to the app (a private address) doesn't count either."""
    monkeypatch.setenv("TRUST_PROXY", "2")
    jf.set_policy(EnableRemoteAccess=False)
    client.post("/login", data={"username": "alice", "password": PASSWORD},
                headers={"X-Forwarded-For": "192.168.77.5, 8.8.4.4, 172.18.0.3"})
    assert jf.auth_calls == []
    assert flashes(client) == [WRONG]

    lan = app_module.app.test_client()
    lan.post("/login", data={"username": "alice", "password": PASSWORD},
             headers={"X-Forwarded-For": "8.8.4.4, 192.168.77.5, 172.18.0.3"})
    assert len(jf.auth_calls) == 1


def test_two_proxies_short_chain_is_not_mistaken_for_the_lan(client, jf, monkeypatch):
    """TRUST_PROXY=2, but this request reached the inner proxy directly, so it
    added the only entry. The connecting address is that proxy's — a private
    one — and trusting it would let an internet client past the remote-access
    check and put every such client on one throttle counter."""
    monkeypatch.setenv("TRUST_PROXY", "2")
    jf.set_policy(EnableRemoteAccess=False)
    client.post("/login", data={"username": "alice", "password": PASSWORD},
                headers={"X-Forwarded-For": "203.0.113.50"})
    assert jf.auth_calls == []
    assert flashes(client) == [WRONG]
    assert app_module.login_limiter.ip_failures("203.0.113.50") == 1
    assert app_module.login_limiter.ip_failures(IP) == 0   # ...against the client, not the proxy


def test_two_proxies_revalidation_uses_the_client_address(client, jf, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "2")
    jf.set_policy(EnableRemoteAccess=False)
    _due(client)
    r = client.get("/api/scan/state", headers={"X-Forwarded-For": "192.168.77.5, 8.8.4.4, 172.18.0.3"})
    assert r.status_code == 401


def test_authenticate_403_reads_like_a_wrong_password(client, jf):
    """Jellyfin answers 403 for a disabled account, remote access off, or the
    parental schedule — whatever the password. Saying so would tell anyone
    the account exists, so it reads (and costs) like a wrong password."""
    jf.auth_status = 403
    login(client)
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert len(jf.auth_calls) == 1
    assert app_module.login_limiter.ip_failures(IP) == 1
    assert flashes(client) == [WRONG]


def test_uid_is_stored_from_the_sign_in(client, jf):
    jf.user = {"Name": "alice", "Id": "abc123"}
    jf.users[0]["Id"] = "abc123"
    login(client)
    with client.session_transaction() as sess:
        assert sess["uid"] == "abc123"


def test_sign_in_without_any_user_id_is_not_a_session(client, jf):
    jf.user = {"Name": "alice"}
    del jf.users[0]["Id"]
    login(client)
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True
    assert flashes(client) == [UNREACHABLE]


# ------------------------------------------------------------------ no account enumeration
# The page must not reveal which usernames exist. A LAN-only account used to
# answer "can't sign in right now (disabled or restricted)" 20 times out of
# 20 for free, while a made-up name cost an attempt — and a made-up name
# failed in ~10 ms while a real one waited ~260 ms on the password check.


def _no_such_user(jf, monkeypatch):
    jf.users = []


def _wrong_password(jf, monkeypatch):
    jf.auth_status = 401


def _disabled(jf, monkeypatch):
    jf.set_policy(IsDisabled=True)


def _one_from_lockout(jf, monkeypatch):
    jf.set_policy(LoginAttemptsBeforeLockout=3, InvalidLoginAttemptCount=2)


def _locks_on_first_failure(jf, monkeypatch):
    jf.set_policy(LoginAttemptsBeforeLockout=1)


def _lan_only_from_outside(jf, monkeypatch):
    jf.set_policy(EnableRemoteAccess=False)
    monkeypatch.setattr(app_module, "client_ip", lambda: PUBLIC_IP)


def _jellyfin_says_403(jf, monkeypatch):
    jf.auth_status = 403


@pytest.mark.parametrize("setup", [
    _no_such_user, _wrong_password, _disabled, _one_from_lockout, _locks_on_first_failure,
    _lan_only_from_outside, _jellyfin_says_403,
])
def test_every_account_refusal_reads_and_costs_like_a_wrong_password(client, jf, monkeypatch, setup):
    setup(jf, monkeypatch)
    ip = app_module.client_ip() if setup is _lan_only_from_outside else IP
    login(client)
    assert flashes(client) == [WRONG]
    assert app_module.login_limiter.ip_failures(ip) == 1
    now = time.time()
    assert app_module.login_limiter.user.recent("alice", now) == 1
    with client.session_transaction() as sess:
        assert sess.get("auth") is not True


def test_refusals_without_a_password_check_take_as_long_as_one(client, monkeypatch):
    """Made-up names and accounts refused up front wait about as long as a real
    password check before answering. (Real clock: the only honest test.)"""
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.setattr(app_module, "password_check_timer", PasswordCheckTimer())

    class SlowCheck(JellyfinAuthMock):
        def __init__(self):
            super().__init__()
            self.auth_status = 401
            self.users = [{"Name": "alice", "Id": "u-a", "Policy": default_policy()},
                          {"Name": "bob", "Id": "u-b", "Policy": default_policy(IsDisabled=True)},
                          {"Name": "kid", "Id": "u-k", "Policy": default_policy(EnableRemoteAccess=False)}]

        def post(self, url, **kwargs):
            if url.endswith("/Users/AuthenticateByName"):
                time.sleep(0.25)
            return super().post(url, **kwargs)

    mock = SlowCheck()
    monkeypatch.setattr(app_module.requests, "post", mock.post)
    monkeypatch.setattr(app_module.requests, "get", mock.get)

    def timed(user, n):
        c = app_module.app.test_client()
        started = time.monotonic()
        c.post("/login", data={"username": user, "password": "wrong"},
               headers={"X-Forwarded-For": f"198.51.100.{n}"})
        return time.monotonic() - started

    real = [timed("alice", n) for n in range(3)]           # these are measured
    probes = [timed(user, 10 + n) for n, user in enumerate(("nosuchuser", "zzz", "bob", "kid"))]
    assert len(mock.auth_calls) == 3
    assert min(real) >= 0.24
    assert min(probes) >= 0.2, probes


# ------------------------------------------------------------------ an answer that never arrived
# A read timeout (or a connection dropped after sending) on AuthenticateByName
# is not "unreachable": the password reached Jellyfin, which counts a wrong
# one when it finishes — after the next sign-in may already have read the old
# count and passed the account-disable guard.


class LaggingJellyfin(JellyfinAuthMock):
    """AuthenticateByName outlives the page's timeout: the page gets a
    ReadTimeout, and Jellyfin counts the (wrong) password when it finishes —
    just after the NEXT sign-in has read the Policy."""

    def __init__(self, exc=None):
        super().__init__()
        self.exc = exc or real_requests.exceptions.ReadTimeout("read timed out")
        self.in_flight = 0
        self.set_policy(LoginAttemptsBeforeLockout=3, InvalidLoginAttemptCount=0)

    def post(self, url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            self.calls.append((url, kwargs))
            self.in_flight += 1
            raise self.exc
        return super().post(url, **kwargs)

    def get(self, url, **kwargs):
        resp = super().get(url, **kwargs)   # read first: the count hasn't caught up
        self.land()
        return resp

    def land(self):
        policy = self.users[0]["Policy"]
        while self.in_flight:
            self.in_flight -= 1
            policy["InvalidLoginAttemptCount"] += 1
            if policy["InvalidLoginAttemptCount"] >= policy["LoginAttemptsBeforeLockout"] > 0:
                policy["IsDisabled"] = True


def _lagging(monkeypatch, exc=None):
    mock = LaggingJellyfin(exc)
    monkeypatch.setattr(app_module.requests, "post", mock.post)
    monkeypatch.setattr(app_module.requests, "get", mock.get)
    return mock


@pytest.mark.parametrize("exc", [
    real_requests.exceptions.ReadTimeout("read timed out"),
    real_requests.exceptions.ConnectionError(
        "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"),
])
def test_a_slow_jellyfin_cannot_be_walked_into_disabling_the_account(client, monkeypatch, exc):
    """Repro: limit 3, AuthenticateByName slower than our timeout. Every try
    said "can't reach", none counted, and the third one disabled the account."""
    mock = _lagging(monkeypatch, exc)
    for _ in range(5):
        login(client, password="guess")
    mock.land()
    policy = mock.users[0]["Policy"]
    assert policy["IsDisabled"] is False
    assert policy["InvalidLoginAttemptCount"] == 2
    assert len(mock.auth_calls) == 2
    # 1-2: no answer. 3: Jellyfin shows 1, but 2 are unanswered: held. 4-5:
    # Jellyfin shows 2 of 3 — the ordinary refusal, which reads (and costs)
    # like a wrong password.
    assert flashes(client) == [UNREACHABLE] * 3 + [
        "Wrong username or password. 2 attempts remaining.",
        "Wrong username or password. 1 attempt remaining.",
    ]
    # An unanswered sign-in told the client nothing, so it costs the IP
    # nothing (only the two ordinary refusals count)...
    assert app_module.login_limiter.ip_failures(IP) == 2
    # ...but it probably counted in Jellyfin, so it counts against the account.
    assert app_module.login_limiter.user.recent("alice", time.time()) == 2 + 2


def test_the_hold_ends_after_its_time(client, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(app_module, "pending_sign_ins", PendingSignIns(clock=clock))
    mock = _lagging(monkeypatch)
    login(client, password="guess")
    login(client, password="guess")            # count 0 read, + 1 pending: still room
    login(client, password="guess")            # count 1 + 2 pending: held
    assert len(mock.auth_calls) == 2
    mock.users[0]["Policy"]["InvalidLoginAttemptCount"] = 0   # e.g. signed in on Jellyfin itself
    clock.advance(app_module.pending_sign_ins.hold + 1)
    mock.exc = None
    mock.post = JellyfinAuthMock.post.__get__(mock)             # Jellyfin is fast again
    monkeypatch.setattr(app_module.requests, "post", mock.post)
    r = login(client)
    assert "/login" not in r.headers["Location"]


@pytest.mark.parametrize("status", [502, 504])
def test_a_gateway_timeout_in_front_of_jellyfin_is_held_too(client, jf, status):
    """A proxy in front of Jellyfin that gives up (504) or loses the upstream
    mid-request (502) leaves the same doubt as our own read timeout."""
    jf.set_policy(LoginAttemptsBeforeLockout=3)
    jf.auth_status = status
    for _ in range(3):
        login(client)
    assert len(jf.auth_calls) == 2
    assert app_module.login_limiter.ip_failures(IP) == 0
    assert flashes(client) == [UNREACHABLE] * 3


@pytest.mark.parametrize("exc", [real_requests.exceptions.ConnectTimeout("connect timed out"),
                                 real_requests.exceptions.ConnectionError("refused")])
def test_a_sign_in_that_never_left_is_not_held_or_counted(client, jf, exc):
    jf.set_policy(LoginAttemptsBeforeLockout=3)
    jf.auth_exc = exc
    for _ in range(4):
        login(client)
    assert len(jf.auth_calls) == 4
    assert app_module.login_limiter.user.recent("alice", time.time()) == 0
    assert app_module.pending_sign_ins.count("user-1") == 0


def test_accounts_without_a_lockout_are_never_held(client, monkeypatch):
    mock = _lagging(monkeypatch)
    mock.set_policy(LoginAttemptsBeforeLockout=-1)
    for _ in range(4):
        login(client)
    assert len(mock.auth_calls) == 4


# ------------------------------------------------------------------ logging


def _setup_wrong_password(jf, client):
    jf.auth_status = 401


def _setup_unknown_user(jf, client):
    jf.users = []


def _setup_disabled(jf, client):
    jf.set_policy(IsDisabled=True)


def _setup_lockout_risk(jf, client):
    jf.set_policy(LoginAttemptsBeforeLockout=3, InvalidLoginAttemptCount=2)


def _setup_403(jf, client):
    jf.auth_status = 403


def _setup_ip_locked(jf, client):
    for _ in range(3):
        app_module.login_limiter.failed(IP, "someone")


def _setup_user_locked(jf, client):
    for i in range(6):
        app_module.login_limiter.failed(f"198.51.100.{i}", "alice")


def _setup_unreachable(jf, client):
    jf.users_exc = real_requests.exceptions.ConnectionError("down")


@pytest.mark.parametrize("setup", [
    _setup_wrong_password, _setup_unknown_user, _setup_disabled, _setup_lockout_risk,
    _setup_403, _setup_ip_locked, _setup_user_locked, _setup_unreachable,
])
def test_every_failed_or_refused_login_is_logged_without_the_password(client, jf, caplog, setup):
    setup(jf, client)
    with caplog.at_level(logging.WARNING):
        login(client, username="alice", password="hunter2-SECRET")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "no WARNING logged"
    text = "\n".join(r.getMessage() for r in warnings)
    assert "alice" in text
    assert IP in text
    assert "hunter2-SECRET" not in caplog.text


@pytest.mark.parametrize("form,unset", [
    ({"username": "alice"}, None),                               # no password
    ({"password": "hunter2-SECRET"}, None),                      # no username
    ({"username": "alice", "password": ""}, None),
    ({"username": "alice", "password": "hunter2-SECRET"}, "JELLYFIN_URL"),
    ({"username": "alice", "password": "hunter2-SECRET"}, "JELLYFIN_API_KEY"),
])
def test_refusals_before_any_jellyfin_call_are_logged_too(client, jf, monkeypatch, caplog, form, unset):
    """Every refused sign-in leaves a WARNING line — including the ones turned
    away before Jellyfin is asked (missing fields, missing configuration)."""
    if unset:
        monkeypatch.setattr(app_module, unset, "")
    with caplog.at_level(logging.WARNING):
        client.post("/login", data=form)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(IP in m and "refused" in m for m in warnings), warnings
    if form.get("username"):
        assert any("alice" in m for m in warnings)
    if unset:
        assert any(unset in m for m in warnings)
    assert "hunter2-SECRET" not in caplog.text
    assert jf.auth_calls == [] and jf.gets == []


def test_successful_login_logs_no_password(client, jf, caplog):
    with caplog.at_level(logging.DEBUG):
        login(client, password="hunter2-SECRET")
    assert "hunter2-SECRET" not in caplog.text


def test_log_lines_cannot_be_forged_through_the_username(client, jf, caplog):
    jf.auth_status = 401
    jf.users.append({"Name": "x\nFAKE login ok", "Id": "u9", "Policy": {}})
    with caplog.at_level(logging.WARNING):
        login(client, username="x\nFAKE login ok")
    assert "\nFAKE login ok" not in caplog.text


# ------------------------------------------------------------------ the limiter, end to end


class OwnAccountJellyfin(JellyfinAuthMock):
    """alice, bob and mallory exist; only mallory's own password is right."""

    def __init__(self):
        super().__init__()
        self.users = [{"Name": n, "Id": f"u-{n}", "Policy": default_policy()}
                      for n in ("alice", "bob", "mallory")]

    def post(self, url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            body = kwargs["json"]
            right = (body["Username"], body["Pw"]) == ("mallory", "mine")
            self.auth_status = 200 if right else 401
            self.user = {"Name": body["Username"], "Id": f"u-{body['Username']}"}
        return super().post(url, **kwargs)


def test_signing_in_as_yourself_does_not_reset_the_ip_lock(client, monkeypatch):
    """2 guesses at other people's passwords, sign in as yourself, repeat:
    before, every success wiped the IP's count and 8 of 8 guesses reached
    Jellyfin. The count survives, so the third wrong guess locks the IP."""
    mock = OwnAccountJellyfin()
    monkeypatch.setattr(app_module.requests, "post", mock.post)
    monkeypatch.setattr(app_module.requests, "get", mock.get)

    for rnd in range(4):
        for victim in ("alice", "bob"):
            login(client, username=victim, password=f"guess-{rnd}")
        login(client, username="mallory", password="mine")
        client.post("/logout")

    wrong = [c for c in mock.auth_calls if c[1]["json"]["Username"] != "mallory"]
    assert len(wrong) == 3
    assert app_module.login_limiter.ip_locked(IP) == pytest.approx(3600, abs=5)


def test_an_ipv6_client_cannot_rotate_addresses_inside_its_64(client, jf, monkeypatch):
    """A password spray from fresh addresses in one /64 (TRUST_PROXY passes
    the full client address): all 30 guesses got through before."""
    monkeypatch.setenv("TRUST_PROXY", "1")
    jf.auth_status = 401
    jf.users = [{"Name": f"user{i}", "Id": f"u{i}", "Policy": default_policy()} for i in range(10)]
    n = 0
    for i in range(10):
        for _ in range(3):
            n += 1
            client.post("/login", data={"username": f"user{i}", "password": f"Summer2026!{n}"},
                        headers={"X-Forwarded-For": f"2001:db8:5a3c:8e10::{n:x}"})
    assert len(jf.auth_calls) == 3
    assert app_module.login_limiter.ip_locked("2001:db8:5a3c:8e10:1234::1") > 0
    assert app_module.login_limiter.ip_locked("2001:db8:5a3c:8e11::1") == 0


# ------------------------------------------------------------------ concurrency
# The limiter check, the Policy read, the Jellyfin call and recording the
# verdict must be one atomic step. Otherwise N parallel wrong-password posts
# all pass the checks before any failure is counted.


class CountingJellyfin(JellyfinAuthMock):
    """Behaves like Jellyfin: every rejected password bumps the account's
    InvalidLoginAttemptCount; reaching LoginAttemptsBeforeLockout disables it.
    AuthenticateByName is slow, which is what leaves the race window open."""

    def post(self, url, **kwargs):
        if url.endswith("/Users/AuthenticateByName"):
            time.sleep(0.05)
            resp = super().post(url, **kwargs)
            if resp.status_code == 401:
                policy = self.users[0]["Policy"]
                policy["InvalidLoginAttemptCount"] += 1
                if policy["InvalidLoginAttemptCount"] >= policy["LoginAttemptsBeforeLockout"] > 0:
                    policy["IsDisabled"] = True
            return resp
        return super().post(url, **kwargs)

    def get(self, url, **kwargs):
        if url.endswith("/Users"):
            time.sleep(0.02)  # a real round trip
        return super().get(url, **kwargs)


def _parallel(n, target):
    import threading

    barrier = threading.Barrier(n)
    errors = []

    def run(i):
        try:
            barrier.wait()
            target(i)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_parallel_guesses_from_one_ip_still_get_three(client, monkeypatch):
    mock = CountingJellyfin()
    mock.auth_status = 401
    monkeypatch.setattr(real_requests, "post", mock.post)
    monkeypatch.setattr(real_requests, "get", mock.get)

    _parallel(12, lambda i: app_module.app.test_client().post(
        "/login", data={"username": f"user{i}", "password": "guess"}))
    # Unknown usernames never reach AuthenticateByName, so count the user
    # lookups: each is one guess that cost an attempt.
    assert len(mock.user_list_calls) == 3
    assert app_module.login_limiter.ip_locked(IP) > 0


def test_parallel_guesses_cannot_trip_jellyfins_account_lockout(client, monkeypatch):
    """The account-disable guard must hold under a burst from many IPs."""
    monkeypatch.setenv("TRUST_PROXY", "1")
    mock = CountingJellyfin()
    mock.auth_status = 401
    mock.set_policy(LoginAttemptsBeforeLockout=3, InvalidLoginAttemptCount=0)
    monkeypatch.setattr(real_requests, "post", mock.post)
    monkeypatch.setattr(real_requests, "get", mock.get)

    _parallel(10, lambda i: app_module.app.test_client().post(
        "/login", data={"username": "alice", "password": f"guess-{i}"},
        headers={"X-Forwarded-For": f"8.8.4.{i}"}))

    assert mock.users[0]["Policy"]["IsDisabled"] is False
    assert len(mock.auth_calls) == 2  # stops one short of Jellyfin's limit


def test_a_sign_in_that_waits_too_long_is_turned_away_for_free(client, jf, monkeypatch):
    monkeypatch.setattr(app_module, "LOGIN_QUEUE_SECONDS", 0.05)
    assert app_module._login_lock.acquire(timeout=1)
    try:
        login(client)
    finally:
        app_module._login_lock.release()
    assert jf.auth_calls == [] and jf.gets == []
    assert app_module.login_limiter.ip_failures(IP) == 0
    assert flashes(client) == ["The server is busy with other sign-ins. Try again in a moment."]
