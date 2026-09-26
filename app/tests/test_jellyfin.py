"""The shared Jellyfin API helpers (app/jellyfin.py)."""

import pytest
import requests

import jellyfin as jf
from conftest import FakeResponse

BASE = "http://jellyfin.test"


class Recorder:
    """A requests.get/post stand-in that records calls and answers from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


# ------------------------------------------------------------------ headers


def test_api_headers_use_the_mediabrowser_authorization_token():
    headers = jf.api_headers("k3y")
    assert headers["Accept"] == "application/json"
    auth = headers["Authorization"]
    assert auth.startswith('MediaBrowser Client="Jellyfin Manager"')
    assert auth.endswith(', Token="k3y"')
    # The legacy header is ignored by newer Jellyfin; never rely on it.
    assert "X-Emby-Token" not in headers


def test_client_header_without_token_has_no_token_field():
    assert "Token=" not in jf.client_header()
    assert jf.client_header("abc").endswith(', Token="abc"')


# ------------------------------------------------------------------ get_json / post


def test_get_json_sends_auth_disables_redirects_and_returns_body():
    get = Recorder(FakeResponse(payload={"ok": 1}))
    assert jf.get_json(BASE, "k", "/System/Info", params={"a": 1}, get=get) == {"ok": 1}
    url, kwargs = get.calls[0]
    assert url == BASE + "/System/Info"
    assert kwargs["params"] == {"a": 1}
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Authorization"].endswith('Token="k"')
    assert kwargs["timeout"] > 0


@pytest.mark.parametrize("answer,reason", [
    (requests.ConnectTimeout("http://10.0.0.5:8096 api_key=k"), "Jellyfin timed out"),
    (requests.ReadTimeout(), "Jellyfin timed out"),
    (requests.ConnectionError("HTTPConnectionPool(host='10.0.0.5')"), "Jellyfin unreachable"),
    (FakeResponse(status_code=401), "Jellyfin rejected the API key (HTTP 401)"),
    (FakeResponse(status_code=403), "Jellyfin rejected the API key (HTTP 403)"),
    (FakeResponse(status_code=302), "Jellyfin redirected (HTTP 302) - check JELLYFIN_URL"),
    (FakeResponse(status_code=500), "Jellyfin returned HTTP 500"),
    (FakeResponse(payload=ValueError("<html>")), "Jellyfin returned an unreadable response"),
])
def test_failures_become_short_safe_reasons(answer, reason):
    with pytest.raises(jf.JfError) as info:
        jf.get_json(BASE, "k", "/x", get=Recorder(answer))
    assert str(info.value) == reason
    assert "10.0.0.5" not in str(info.value) and "api_key" not in str(info.value)


def test_unconfigured_is_an_error_without_a_request():
    get = Recorder(FakeResponse(payload={}))
    for base, key in (("", "k"), (BASE, "")):
        with pytest.raises(jf.JfError, match="not configured"):
            jf.get_json(base, key, "/x", get=get)
    assert get.calls == []


def test_post_accepts_2xx_and_rejects_redirects():
    post = Recorder(FakeResponse(status_code=204))
    jf.post(BASE, "k", "/Library/Refresh", post=post)
    url, kwargs = post.calls[0]
    assert url == BASE + "/Library/Refresh"
    assert kwargs["allow_redirects"] is False
    with pytest.raises(jf.JfError, match=r"^Jellyfin redirected \(HTTP 301\)"):
        jf.post(BASE, "k", "/Library/Refresh", post=Recorder(FakeResponse(status_code=301)))


# ------------------------------------------------------------------ scheduled tasks


def test_library_task_selects_by_key_not_name():
    tasks = [
        {"Name": "Refresh Guide", "Key": "RefreshGuide", "State": "Running"},
        "junk",
        {"Name": "Medienbibliothek scannen", "Key": "RefreshLibrary", "State": "Idle"},
    ]
    task = jf.library_task(BASE, "k", get=Recorder(FakeResponse(payload=tasks)))
    assert task["Name"] == "Medienbibliothek scannen"


def test_library_task_missing_is_none_and_garbage_is_error():
    assert jf.library_task(BASE, "k", get=Recorder(FakeResponse(payload=[]))) is None
    with pytest.raises(jf.JfError):
        jf.library_task(BASE, "k", get=Recorder(FakeResponse(payload={"not": "a list"})))


@pytest.mark.parametrize("state,busy", [("Running", True), ("Cancelling", True), ("Idle", False), (None, False)])
def test_task_busy(state, busy):
    assert jf.task_busy({"State": state}) is busy
    assert jf.task_busy(None) is False


# ------------------------------------------------------------------ users


def test_find_user_is_case_insensitive():
    users = [{"Name": "Alice", "Id": "1"}, {"Name": "bob", "Id": "2"}, "junk"]
    assert jf.find_user(users, "alice")["Id"] == "1"
    assert jf.find_user(users, "BOB")["Id"] == "2"
    assert jf.find_user(users, "carol") is None


def test_list_users_requires_a_list():
    assert jf.list_users(BASE, "k", get=Recorder(FakeResponse(payload=[{"Name": "a"}]))) == [{"Name": "a"}]
    with pytest.raises(jf.JfError):
        jf.list_users(BASE, "k", get=Recorder(FakeResponse(payload={})))


def test_get_user_404_is_none():
    assert jf.get_user(BASE, "k", "u1", get=Recorder(FakeResponse(status_code=404))) is None
    assert jf.get_user(BASE, "k", "u1", get=Recorder(FakeResponse(payload={"Id": "u1"}))) == {"Id": "u1"}


def test_get_user_other_failures_raise():
    with pytest.raises(jf.JfError):
        jf.get_user(BASE, "k", "u1", get=Recorder(requests.ConnectionError()))


# ------------------------------------------------------------------ time


def test_parse_time_handles_seven_digit_fraction_and_z():
    assert jf.parse_time("2026-09-26T14:03:11.1234567Z") == pytest.approx(1790431391.123456)


def test_parse_time_offsets_and_naive():
    assert jf.parse_time("2026-09-26T16:03:11+02:00") == jf.parse_time("2026-09-26T14:03:11Z")
    assert jf.parse_time("2026-09-26T14:03:11") == jf.parse_time("2026-09-26T14:03:11Z")


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-13-40T99:99:99Z", 12345,
                                 "2026-09-26T14:03:11+24:00", "2026-09-26T14:03:11+23:99"])
def test_parse_time_garbage_is_none(bad):
    assert jf.parse_time(bad) is None


# ------------------------------------------------------------------ ambiguous POSTs


@pytest.mark.parametrize("exc", [
    requests.ReadTimeout("read timed out"),
    requests.ConnectionError(requests.packages.urllib3.exceptions.ProtocolError(
        "Connection aborted.", ConnectionResetError("reset"))),
])
def test_post_that_may_have_been_delivered_is_ambiguous(exc):
    # Jellyfin got the request but the answer never arrived: a retry would
    # queue a second Refresh, which restarts the running scan from 0%.
    with pytest.raises(jf.JfAmbiguous) as info:
        jf.post(BASE, "k", "/Library/Refresh", post=Recorder(exc))
    assert isinstance(info.value, jf.JfError)
    assert str(info.value) == "Jellyfin was slow to answer; the scan was probably started"


@pytest.mark.parametrize("exc", [requests.ConnectTimeout(), requests.ConnectionError("refused")])
def test_post_that_never_left_is_a_plain_error(exc):
    with pytest.raises(jf.JfError) as info:
        jf.post(BASE, "k", "/Library/Refresh", post=Recorder(exc))
    assert not isinstance(info.value, jf.JfAmbiguous)


# ------------------------------------------------------------------ last run


def test_last_result_reports_status_and_end_time():
    task = {"LastExecutionResult": {"Status": "Cancelled", "EndTimeUtc": "2026-09-26T14:03:11.1234567Z",
                                    "StartTimeUtc": "2026-09-26T14:00:00Z"}}
    assert jf.last_result(task) == {"status": "Cancelled", "started_at": 1790431200.0,
                                    "ended_at": pytest.approx(1790431391.123456)}


@pytest.mark.parametrize("task", [None, {}, {"LastExecutionResult": None}, {"LastExecutionResult": "x"}])
def test_last_result_missing_is_none(task):
    assert jf.last_result(task) is None
