import atexit
import copy
import importlib
import os
import shutil
import sys
import tempfile
import time

# Make the app package dir (app/) importable so `import app` / `import history`
# resolve to app/app.py and app/history.py.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import pytest

# app.py probes DATA_DIR when it is imported (and on every reload). Point it at
# a throwaway directory first: the default /data doesn't exist on a dev box, so
# the probe would log a spurious ERROR and start the whole run "unwritable" —
# and it must never touch a real history file either.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="jfm-test-data-")
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)
os.environ["DATA_DIR"] = _TEST_DATA_DIR

import app as app_module  # noqa: E402
from history import ScanHistory
from limiter import LoginLimiter, PasswordCheckTimer, PendingSignIns


class FakeResponse:
    def __init__(self, payload=None, exc=None, status_code=200):
        self._payload = payload
        self._exc = exc
        self.status_code = status_code

    def raise_for_status(self):
        if self._exc:
            raise self._exc

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def default_policy(**overrides):
    """A Jellyfin UserPolicy with nothing that blocks a sign-in."""
    policy = {
        "IsDisabled": False,
        "InvalidLoginAttemptCount": 0,
        "LoginAttemptsBeforeLockout": -1,  # Jellyfin's "no lockout configured"
        "EnableRemoteAccess": True,
    }
    policy.update(overrides)
    return policy


class JellyfinAuthMock:
    """Fake Jellyfin: the login endpoints (AuthenticateByName + Sessions/Logout),
    the admin-key user lookups the login guard makes (GET /Users,
    GET /Users/{id}), and the scan endpoints (GET /ScheduledTasks,
    POST /Library/Refresh).

    Records every call so tests can assert exactly what left the app: where the
    password went, which headers were sent, whether the token was revoked, and
    whether a refused sign-in ever reached AuthenticateByName.
    """

    def __init__(self):
        self.calls = []  # (url, kwargs) for every POST, in order
        self.gets = []   # (url, kwargs) for every GET, in order
        self.auth_status = 200
        self.user = {"Name": "alice", "Id": "user-1"}
        self.access_token = "tok-abc123"
        self.auth_exc = None    # raised instead of answering AuthenticateByName
        self.logout_exc = None  # raised on Sessions/Logout
        # What GET /Users (admin API key) lists. The login guard reads Policy.
        self.users = [{"Name": "alice", "Id": "user-1", "Policy": default_policy()}]
        self.users_exc = None     # raised on GET /Users and GET /Users/{id}
        self.users_status = 200   # status for GET /Users and GET /Users/{id}
        self.tasks = []           # GET /ScheduledTasks

    def set_policy(self, **overrides):
        """Adjust alice's Policy as GET /Users reports it."""
        self.users[0]["Policy"] = default_policy(**overrides)

    @property
    def auth_calls(self):
        return [c for c in self.calls if c[0].endswith("/Users/AuthenticateByName")]

    @property
    def logout_calls(self):
        return [c for c in self.calls if c[0].endswith("/Sessions/Logout")]

    @property
    def refresh_calls(self):
        return [c for c in self.calls if c[0].endswith("/Library/Refresh")]

    @property
    def user_list_calls(self):
        return [g for g in self.gets if g[0].endswith("/Users")]

    @property
    def user_get_calls(self):
        return [g for g in self.gets if "/Users/" in g[0]]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/Users/AuthenticateByName"):
            if self.auth_exc:
                raise self.auth_exc
            if self.auth_status == 200:
                return FakeResponse(payload={"User": dict(self.user), "AccessToken": self.access_token})
            return FakeResponse(status_code=self.auth_status)
        if url.endswith("/Sessions/Logout"):
            if self.logout_exc:
                raise self.logout_exc
            return FakeResponse(status_code=204)
        if url.endswith("/Library/Refresh"):
            return FakeResponse(status_code=204)
        raise AssertionError(f"unexpected POST to {url}")

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        if path == "ScheduledTasks":
            return FakeResponse(payload=copy.deepcopy(self.tasks))
        if path == "Users" or path.startswith("Users/"):
            if self.users_exc:
                raise self.users_exc
            if self.users_status != 200:
                return FakeResponse(status_code=self.users_status)
            if path == "Users":
                return FakeResponse(payload=copy.deepcopy(self.users))
            wanted = path[len("Users/"):]
            for user in self.users:
                if user.get("Id") == wanted:
                    return FakeResponse(payload=copy.deepcopy(user))
            return FakeResponse(status_code=404)
        raise AssertionError(f"unexpected GET to {url}")


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
    # Throttling and logout revocation are in-process state: fresh per test.
    monkeypatch.setattr(app_module, "login_limiter", LoginLimiter())
    monkeypatch.setattr(app_module, "pending_sign_ins", PendingSignIns())
    # Refusals that never ask Jellyfin wait about one password check before
    # answering (anti-enumeration). The mocks answer instantly, so measured
    # checks are ~0; with no measurement yet, don't sleep either.
    monkeypatch.setattr(app_module, "password_check_timer", PasswordCheckTimer(default=0.0))
    monkeypatch.setattr(app_module, "_revoked_sids", {})
    # "Is the history being saved?" is process state too: a test whose record()
    # fails must not leave the next test's page warning about /data.
    monkeypatch.setattr(app_module, "_history_writable", True)
    # ...and each test sees its own once-per-value TRUST_PROXY warning.
    monkeypatch.setattr(app_module, "_trust_proxy_warned", None)
    # The last scan state /api/scan/progress saw (drives the panel's cache).
    monkeypatch.setattr(app_module, "_scan_seen_running", None)
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    app_module.app.secret_key = "test-secret"

    def boom(*a, **kw):  # any unmocked network call is a test bug
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(app_module.requests, "post", boom)
    monkeypatch.setattr(app_module.requests, "get", boom)

    return app_module.app.test_client()


@pytest.fixture
def reload_app(tmp_path):
    """Re-import app.py under a chosen environment (DATA_DIR defaults to a
    fresh tmp dir), then restore the pristine module so no other test sees
    the reloaded globals."""
    saved = dict(os.environ)
    keys = ("SECRET_KEY", "SECURE_COOKIES", "JELLYFIN_URL", "JELLYFIN_API_KEY",
            "TRUST_PROXY", "QBITTORRENT_INSTANCES", "DATA_DIR")

    def _reload(**env):
        for k in keys:
            os.environ.pop(k, None)
        settings = {"DATA_DIR": str(tmp_path), "JELLYFIN_URL": "http://jellyfin.test",
                    "JELLYFIN_API_KEY": "api-key"}
        settings.update(env)
        os.environ.update(settings)
        return importlib.reload(app_module)

    yield _reload
    os.environ.clear()
    os.environ.update(saved)
    importlib.reload(app_module)


def sign_in(sess, uid="user-1", user=None, sid="test-sid"):
    """Make `sess` a signed-in session exactly as a real login leaves it:
    auth + Jellyfin user Id + a random session id, with the Jellyfin
    re-validation not due yet (so no test makes an unplanned GET /Users/{id})."""
    sess["auth"] = True
    sess["uid"] = uid
    sess["sid"] = sid
    sess["recheck_at"] = time.time() + app_module.REVALIDATE_SECONDS
    if user is not None:
        sess["user"] = user


@pytest.fixture
def auth(client):
    with client.session_transaction() as sess:
        sign_in(sess)
    return client


@pytest.fixture
def jf(client, monkeypatch):
    """Mock Jellyfin on the requests module app.py (and jellyfin.py) uses."""
    mock = JellyfinAuthMock()
    monkeypatch.setattr(app_module.requests, "post", mock.post)
    monkeypatch.setattr(app_module.requests, "get", mock.get)
    return mock


def login(client, username="alice", password="s3cret!"):
    return client.post("/login", data={"username": username, "password": password})


def render_context(client, path="/login"):
    """GET `path` and return (status, name -> context of every template it
    rendered). Asserts on what the view hands the template, not on markup the
    frontend is free to change."""
    from flask import template_rendered

    seen = {}

    def record(sender, template, context, **extra):
        seen[template.name] = context

    template_rendered.connect(record, app_module.app)
    try:
        r = client.get(path)
    finally:
        template_rendered.disconnect(record, app_module.app)
    return r.status_code, seen


def flashes(client):
    """Pending flash messages without rendering a template."""
    with client.session_transaction() as sess:
        return [msg for _cat, msg in sess.get("_flashes", [])]
