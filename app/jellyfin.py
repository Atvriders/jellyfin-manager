"""Small, shared Jellyfin API helpers.

Every server-side call to Jellyfin goes through here so that three rules hold
everywhere:

  * Auth uses the MediaBrowser ``Authorization`` header with a ``Token`` field.
    The legacy ``X-Emby-Token`` header is ignored by newer Jellyfin releases.
  * Redirects are never followed. A 3xx would turn a POST into a GET (whose
    2xx looked like success) and hand the admin API key to the redirect host.
  * Failures become a short, fixed-vocabulary reason (``JfError``) that is safe
    to show every signed-in user and to store in the shared history. Raw
    ``requests`` exception text contains the internal Jellyfin host and port.
"""

import re
from datetime import datetime, timedelta, timezone

import requests

CLIENT_NAME = "Jellyfin Manager"
CLIENT_VERSION = "2.1.0"
GET_TIMEOUT = 5
POST_TIMEOUT = 10


class JfError(Exception):
    """A Jellyfin call failed. str(exc) is a short reason safe to display."""


class JfAmbiguous(JfError):
    """The request left and Jellyfin probably acted on it, but no answer arrived
    (read timeout, or the connection dropped after sending). For a scan that
    means "probably started": treat it as started, because retrying would queue
    a second Refresh, and that restarts the running scan from 0%."""


def client_header(token=None):
    """The MediaBrowser Authorization header value, optionally with a Token."""
    value = (
        f'MediaBrowser Client="{CLIENT_NAME}", Device="jellyfin-manager", '
        f'DeviceId="jellyfin-manager", Version="{CLIENT_VERSION}"'
    )
    if token:
        value += f', Token="{token}"'
    return value


def api_headers(api_key):
    return {"Authorization": client_header(api_key), "Accept": "application/json"}


def describe_status(status_code):
    """Short reason for a non-success HTTP status from Jellyfin."""
    if 300 <= status_code < 400:
        return f"Jellyfin redirected (HTTP {status_code}) - check JELLYFIN_URL"
    if status_code in (401, 403):
        return f"Jellyfin rejected the API key (HTTP {status_code})"
    return f"Jellyfin returned HTTP {status_code}"


def describe_exception(exc):
    """Short reason for a requests exception. Never includes the exception text."""
    if isinstance(exc, requests.Timeout):
        return "Jellyfin timed out"
    if isinstance(exc, requests.RequestException):
        return "Jellyfin unreachable"
    return "Jellyfin request failed"


def maybe_delivered(exc):
    """True when the request was sent but its answer was lost.

    ReadTimeout means the connection was up and the request written. A
    ConnectionError wrapping "Connection aborted" (RemoteDisconnected, reset)
    also happens after sending. ConnectTimeout and "refused" never left.
    """
    if isinstance(exc, requests.ReadTimeout):
        return True
    if isinstance(exc, requests.ConnectionError) and not isinstance(exc, requests.ConnectTimeout):
        return "Connection aborted" in str(exc)
    return False


def _check_configured(base_url, api_key):
    if not base_url or not api_key:
        raise JfError("JELLYFIN_URL or JELLYFIN_API_KEY not configured")


def get_json(base_url, api_key, path, params=None, get=None, timeout=GET_TIMEOUT, allow_404=False):
    """GET a Jellyfin JSON endpoint. Returns the parsed body.

    With allow_404, a 404 returns None instead of raising.
    """
    _check_configured(base_url, api_key)
    getter = get or requests.get
    try:
        resp = getter(
            base_url + path,
            params=params,
            headers=api_headers(api_key),
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        raise JfError(describe_exception(exc)) from exc
    if allow_404 and resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise JfError(describe_status(resp.status_code))
    try:
        return resp.json()
    except Exception as exc:
        raise JfError("Jellyfin returned an unreadable response") from exc


def post(base_url, api_key, path, post=None, timeout=POST_TIMEOUT):
    """POST to a Jellyfin endpoint that answers 2xx with no body we need."""
    _check_configured(base_url, api_key)
    poster = post or requests.post
    try:
        resp = poster(base_url + path, headers=api_headers(api_key), timeout=timeout, allow_redirects=False)
    except Exception as exc:
        if maybe_delivered(exc):
            raise JfAmbiguous("Jellyfin was slow to answer; the scan was probably started") from exc
        raise JfError(describe_exception(exc)) from exc
    if not 200 <= resp.status_code < 300:
        raise JfError(describe_status(resp.status_code))


# ---------------------------------------------------------------- scheduled tasks


def library_task(base_url, api_key, get=None):
    """The "Scan Media Library" scheduled task, found by Key (names are localized)."""
    tasks = get_json(base_url, api_key, "/ScheduledTasks", get=get)
    if not isinstance(tasks, list):
        raise JfError("Jellyfin returned an unreadable response")
    for task in tasks:
        if isinstance(task, dict) and task.get("Key") == "RefreshLibrary":
            return task
    return None


def last_result(task):
    """How the task's most recent run ended, or None if Jellyfin didn't say.

    {"status": "Completed" | "Failed" | "Cancelled" | "Aborted", "started_at": epoch|None,
     "ended_at": epoch|None}
    """
    last = task.get("LastExecutionResult") if isinstance(task, dict) else None
    if not isinstance(last, dict) or not isinstance(last.get("Status"), str):
        return None
    return {
        "status": last["Status"],
        "started_at": parse_time(last.get("StartTimeUtc")),
        "ended_at": parse_time(last.get("EndTimeUtc")),
    }


def task_busy(task):
    """True while the task runs or is winding down after a cancel."""
    return isinstance(task, dict) and task.get("State") in ("Running", "Cancelling")


# ---------------------------------------------------------------- users


def list_users(base_url, api_key, get=None):
    users = get_json(base_url, api_key, "/Users", get=get)
    if not isinstance(users, list):
        raise JfError("Jellyfin returned an unreadable response")
    return users


def find_user(users, username):
    """Jellyfin matches user names case-insensitively; so do we."""
    wanted = (username or "").casefold()
    for user in users:
        if isinstance(user, dict) and str(user.get("Name", "")).casefold() == wanted:
            return user
    return None


def get_user(base_url, api_key, user_id, get=None):
    """The user record, or None if Jellyfin no longer has that user."""
    user = get_json(base_url, api_key, f"/Users/{user_id}", get=get, allow_404=True)
    if user is not None and not isinstance(user, dict):
        raise JfError("Jellyfin returned an unreadable response")
    return user


# ---------------------------------------------------------------- time

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_time(value):
    """Jellyfin ISO-8601 timestamp -> epoch seconds, or None.

    Jellyfin emits 7 fractional digits and a 'Z' ("2026-09-26T14:03:11.1234567Z"),
    which Python 3.10's fromisoformat rejects. No offset means UTC (the fields we
    read are all *Utc).
    """
    match = _ISO_RE.match(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    year, month, day, hour, minute, second, frac, tz = match.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    try:
        # Inside the try: an impossible offset (+24:00, +23:99) raises too.
        if tz in (None, "Z"):
            tzinfo = timezone.utc
        else:
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            tzinfo = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                        micro, tzinfo=tzinfo).timestamp()
    except ValueError:
        return None
