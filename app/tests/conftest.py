import os
import sys

# Make the app package dir (app/) importable so `import app` / `import history`
# resolve to app/app.py and app/history.py.
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import pytest

import app as app_module
from history import ScanHistory


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


class JellyfinAuthMock:
    """Fake Jellyfin login endpoints (AuthenticateByName + Sessions/Logout).

    Records every POST so tests can assert exactly what left the app: where the
    password went, which headers were sent, whether the token was revoked.
    """

    def __init__(self):
        self.calls = []  # (url, kwargs) for every POST, in order
        self.auth_status = 200
        self.user = {"Name": "alice", "Id": "user-1"}
        self.access_token = "tok-abc123"
        self.auth_exc = None    # raised instead of answering AuthenticateByName
        self.logout_exc = None  # raised on Sessions/Logout

    @property
    def auth_calls(self):
        return [c for c in self.calls if c[0].endswith("/Users/AuthenticateByName")]

    @property
    def logout_calls(self):
        return [c for c in self.calls if c[0].endswith("/Sessions/Logout")]

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
        raise AssertionError(f"unexpected POST to {url}")


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


@pytest.fixture
def jf(client, monkeypatch):
    """Mock Jellyfin's login endpoints on the requests module app.py uses."""
    mock = JellyfinAuthMock()
    monkeypatch.setattr(app_module.requests, "post", mock.post)
    return mock


def login(client, username="alice", password="s3cret!"):
    return client.post("/login", data={"username": username, "password": password})


def flashes(client):
    """Pending flash messages without rendering a template."""
    with client.session_transaction() as sess:
        return [msg for _cat, msg in sess.get("_flashes", [])]
