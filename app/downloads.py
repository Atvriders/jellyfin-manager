"""The "Incoming" panel: what qBittorrent is downloading, and what finished but
is not in Jellyfin yet (the case where pressing Scan actually helps).

Read-only by design: nothing in this module ever changes a torrent.

"Not in Jellyfin yet" means BOTH:
  * the torrent finished after the last *completed* library scan started (that
    scan saw the folder after the file landed, so it already had its chance), and
  * no item Jellyfin added recently has the torrent's folder/file name as one of
    its path components (real-time monitoring may have picked it up already).

Unknown is never reported as zero: an unreachable qBittorrent box or Jellyfin is
reported as such, never as "nothing downloading" / "nothing waiting".
"""

import math
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import requests

import jellyfin
from jellyfin import JfError

QB_TIMEOUT = 5              # seconds, per qBittorrent request
BUILD_DEADLINE = 12         # seconds; Jellyfin paging (-> "unknown") and file listing stop past this
CACHE_TTL = 10              # seconds a finished payload is reused across viewers
BUILD_WAIT = 15             # seconds a viewer waits on another viewer's build (page gives up at 20)
SKEW_SLACK = 120            # seconds; clock skew between boxes, errs toward showing
FALLBACK_WINDOW = 48 * 3600  # when the last scan can't be trusted
MATCH_WINDOW_PAD = 24 * 3600
ITEMS_PAGE = 200
ITEMS_CAP = 2000
LIST_CAP = 25
ETA_INFINITY = 8640000      # qBittorrent's "no ETA"
MAX_EPOCH = 4102444800      # 2100-01-01; anything later is garbage

# The ONLY error strings that ever leave the server for a qBittorrent source.
# Raw exception text can carry URLs and credentials, so it is never returned.
ERR_UNREACHABLE = "unreachable"
ERR_LOGIN = "login failed"
ERR_BAD = "unexpected response"

UP_STATES = frozenset({
    "uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP", "pausedUP", "stoppedUP",
})
STATE_MAP = {
    "downloading": "downloading", "forcedDL": "downloading",
    "metaDL": "metadata", "forcedMetaDL": "metadata",
    "stalledDL": "stalled",
    "queuedDL": "queued",
    "checkingDL": "checking", "checkingResumeData": "checking",
    "allocating": "checking", "moving": "checking",
    "pausedDL": "paused", "stoppedDL": "paused",
    "error": "error", "missingFiles": "error",
}
STATE_ORDER = ["downloading", "metadata", "checking", "stalled", "stuck", "error", "queued", "paused"]


class QbError(Exception):
    """A qBittorrent source failed. str(exc) is one of the ERR_* categories."""


class Busy(Exception):
    """Another viewer's build is still running and there is no earlier payload
    to show instead."""


# ---------------------------------------------------------------- config


@dataclass(frozen=True)
class Instance:
    label: str
    url: str            # scheme://host[:port][/path], credentials removed, no trailing slash
    username: str = ""
    password: str = ""


def parse_instances(raw, warn=None):
    """Parse QBITTORRENT_INSTANCES ("Label=http://host:8080, Other=http://...").

    Bad entries are reported through `warn` (never with the URL: it may hold a
    password) and skipped; the valid ones still work.
    """
    warn = warn or (lambda msg: None)
    instances, seen = [], set()
    for index, part in enumerate((raw or "").split(",")):
        part = part.strip()
        if not part:
            continue
        label, sep, url = part.partition("=")
        label, url = label.strip(), url.strip()
        if not sep or not label or not url:
            warn(f"QBITTORRENT_INSTANCES entry #{index + 1} is not 'label=url'; skipped")
            continue
        try:
            parts = urlsplit(url)
            host_ok = bool(parts.hostname)
        except ValueError:
            host_ok = False
        if not host_ok or parts.scheme not in ("http", "https"):
            warn(f"QBITTORRENT_INSTANCES entry '{label}' needs an http(s)://host URL; skipped")
            continue
        if label in seen:
            warn(f"QBITTORRENT_INSTANCES label '{label}' is used twice; the second one is skipped")
            continue
        seen.add(label)
        netloc = parts.netloc.rpartition("@")[2]      # drop any user:pass@
        base = f"{parts.scheme}://{netloc}{parts.path}".rstrip("/")
        instances.append(Instance(
            label=label,
            url=base,
            username=unquote(parts.username or ""),
            password=unquote(parts.password or ""),
        ))
    return instances


# ---------------------------------------------------------------- small helpers


def _num(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _nfc(text):
    return unicodedata.normalize("NFC", text)


def _components(path):
    """Non-empty path components, splitting on both / and \\."""
    return [c for c in re.split(r"[\\/]+", path or "") if c]


def _basename(path):
    comps = _components(path)
    return comps[-1] if comps else ""


# ---------------------------------------------------------------- classification


def is_complete(torrent):
    """qBittorrent's upload-family states are its own "finished" verdict.

    Otherwise a negative amount_left means NOT finished, whatever progress
    says: in qBittorrent amount_left = wanted - done and progress = done /
    wanted, unclamped, so the ENOSPC self-lock (done > wanted, the "Unexpected
    data detected" warning) reports progress > 1. Checked before progress, or
    the stuck torrent lands among the complete ones (completion_on -1, so not
    ready either) and silently vanishes from the panel.
    """
    if torrent.get("state") in UP_STATES:
        return True
    if _num(torrent.get("amount_left")) < 0:
        return False
    return _num(torrent.get("progress")) >= 1


def display_state(torrent):
    # A negative amount_left is the ENOSPC self-lock: qBittorrent thinks it has
    # more than it needs and never requests the missing piece. Recheck fixes it.
    if _num(torrent.get("amount_left")) < 0:
        return "stuck"
    mapped = STATE_MAP.get(torrent.get("state"))
    if mapped:
        return mapped
    return "downloading" if _num(torrent.get("dlspeed")) > 0 else "stalled"


def completion_time(torrent):
    """Epoch seconds the torrent finished, or None when qBittorrent doesn't know."""
    value = _num(torrent.get("completion_on"), -1)
    return value if 0 < value < MAX_EPOCH else None


def _added_time(torrent, finished):
    """When the torrent was added; falls back to its finish time if that's garbage."""
    added = _num(torrent.get("added_on"), -1)
    return added if 0 < added <= finished else finished


def downloading_row(source, torrent):
    progress = min(1.0, max(0.0, _num(torrent.get("progress"))))
    eta = _num(torrent.get("eta"), -1)
    return {
        "source": source,
        "name": str(torrent.get("name") or ""),
        "progress": progress,
        "state": display_state(torrent),
        "dlspeed": max(0, int(_num(torrent.get("dlspeed")))),
        "eta": int(eta) if 0 < eta < ETA_INFINITY else None,
        "size": max(0, int(_num(torrent.get("size")))),
    }


def _downloading_sort_key(row):
    return (STATE_ORDER.index(row["state"]), -row["progress"])


def coverage_point(task, now):
    """Files that finished before this moment were seen by a completed scan.

    Only a run whose Status is "Completed" counts. A failed/cancelled/aborted run
    (or none at all) can't be trusted, so fall back to a fixed 48 h window.
    """
    last = (task or {}).get("LastExecutionResult") or {}
    if isinstance(last, dict) and last.get("Status") == "Completed":
        start = jellyfin.parse_time(last.get("StartTimeUtc"))
        if start is not None:
            return start
    return now - FALLBACK_WINDOW


# ---------------------------------------------------------------- qBittorrent


class QbClient:
    """Minimal read-only qBittorrent WebUI API client for one instance."""

    def __init__(self, instance, session=None):
        self.instance = instance
        self.session = session if session is not None else requests.Session()
        self._logged_in = False

    def _headers(self):
        # qBittorrent's CSRF protection wants a Referer/Origin matching the host.
        return {"Referer": self.instance.url}

    def _login(self):
        resp = self.session.post(
            self.instance.url + "/api/v2/auth/login",
            data={"username": self.instance.username, "password": self.instance.password},
            headers=self._headers(),
            timeout=QB_TIMEOUT,
        )
        # Up to 5.1: 200 "Ok." (a bad password is 200 "Fails."). From 5.2
        # (WebAPI 2.14): 204 No Content, and a bad password is 401. Anything
        # else — "Fails.", 401, 403 (IP banned) — is a failed login.
        ok = resp.status_code == 204 or (
            resp.status_code == 200 and (resp.text or "").strip() == "Ok.")
        if not ok:
            raise QbError(ERR_LOGIN)
        self._logged_in = True

    def _get(self, path, params):
        return self.session.get(self.instance.url + path, params=params,
                                headers=self._headers(), timeout=QB_TIMEOUT)

    def call(self, path, params=None):
        """GET a JSON endpoint. A 403 (or 401) means the session expired: log in again once."""
        try:
            if self.instance.username and not self._logged_in:
                self._login()
            resp = self._get(path, params)
            if resp.status_code in (401, 403):
                if not self.instance.username:
                    raise QbError(ERR_LOGIN)   # no credentials to retry with
                self._logged_in = False
                self._login()
                resp = self._get(path, params)
                if resp.status_code in (401, 403):
                    raise QbError(ERR_LOGIN)
            if resp.status_code != 200:
                raise QbError(ERR_BAD)
            try:
                return resp.json()
            except ValueError as exc:
                raise QbError(ERR_BAD) from exc
        except QbError:
            raise
        except requests.RequestException as exc:
            raise QbError(ERR_UNREACHABLE) from exc
        except Exception as exc:  # anything else is still just "this source failed"
            raise QbError(ERR_BAD) from exc

    def torrents(self):
        data = self.call("/api/v2/torrents/info")
        if not isinstance(data, list):
            raise QbError(ERR_BAD)
        return [t for t in data if isinstance(t, dict)]

    def file_names(self, torrent_hash):
        data = self.call("/api/v2/torrents/files", {"hash": torrent_hash})
        if not isinstance(data, list):
            raise QbError(ERR_BAD)
        return [str(f.get("name") or "") for f in data if isinstance(f, dict)]


def match_keys(client, torrent, list_files=True):
    """Names under which this torrent's content would appear in a Jellyfin path.

    Normally the basename of content_path (plus `name` if it differs). When
    content_path == save_path the torrent has no root folder: its basename would
    be the library folder itself and match everything, so use the file names.
    That costs a request; with list_files=False (out of time) it is skipped.
    """
    content = str(torrent.get("content_path") or "")
    save = str(torrent.get("save_path") or "")
    keys = set()
    if content and content.rstrip("\\/") == save.rstrip("\\/"):
        if not list_files:
            return set()   # no keys -> it stays visible (errs toward showing)
        try:
            keys = {_basename(name) for name in client.file_names(torrent.get("hash"))}
        except QbError:
            keys = set()   # can't tell -> no keys -> it stays visible (errs toward showing)
    else:
        if content:
            keys.add(_basename(content))
        if torrent.get("name"):
            keys.add(str(torrent.get("name")))
    return {_nfc(k) for k in keys if k}


# ---------------------------------------------------------------- Jellyfin


def recent_path_components(jf_url, api_key, window_start, deadline, clock, get=None):
    """Every path component of items Jellyfin added since window_start (newest first)."""
    components = set()
    start = 0
    while start < ITEMS_CAP:
        if clock() > deadline:
            raise JfError("Jellyfin item listing took too long")
        data = jellyfin.get_json(jf_url, api_key, "/Items", params={
            "Recursive": "true",
            "IsFolder": "false",
            "SortBy": "DateCreated",
            "SortOrder": "Descending",
            "Fields": "Path,DateCreated",
            "EnableImages": "false",
            "EnableUserData": "false",
            "EnableTotalRecordCount": "false",
            "StartIndex": start,
            "Limit": ITEMS_PAGE,
        }, get=get)
        batch = data.get("Items") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise JfError("unexpected Items response")
        for item in batch:
            if isinstance(item, dict):
                components.update(_nfc(c) for c in _components(item.get("Path")))
        if len(batch) < ITEMS_PAGE:
            break
        oldest = jellyfin.parse_time(batch[-1].get("DateCreated") if isinstance(batch[-1], dict) else None)
        if oldest is not None and oldest < window_start:
            break
        start += ITEMS_PAGE
    return components


# ---------------------------------------------------------------- the panel


class Incoming:
    """Builds (and caches) the /api/downloads payload.

    One build at a time, shared: viewers that arrive while a build runs get
    that build's result when it lands, however long it took, so N open tabs
    cost one upstream round per CACHE_TTL. Two locks keep that from hurting
    anything else:

      * _build_lock is held for a whole build (seconds, when a box or Jellyfin
        is slow). Waiting on it is bounded (BUILD_WAIT), after which a viewer
        gets the last payload instead — an unbounded wait here once piled up
        server threads until the app stopped answering at all.
      * _lock guards the fields below and is only ever held for a moment.
        invalidate() takes only this one, so a scan press never waits on
        qBittorrent or Jellyfin.

    Cache age is measured on the monotonic clock (a wall-clock step back of X
    seconds froze the payload for X + 10 s); the wall clock is only for the
    timestamps that are compared with qBittorrent's and Jellyfin's.
    """

    def __init__(self, instances, jf_url="", jf_api_key="", session_factory=None,
                 jf_get=None, clock=None, mono=None):
        self.instances = list(instances)
        make_session = session_factory or requests.Session
        self.clients = [QbClient(inst, make_session()) for inst in self.instances]
        self.jf_url = (jf_url or "").rstrip("/")
        self.jf_api_key = jf_api_key or ""
        self._jf_get = jf_get
        self._clock = clock or time.time          # wall: checked_at, coverage point
        self._mono = mono or time.monotonic       # cache age, build deadline
        self._build_lock = threading.Lock()
        self._lock = threading.Lock()
        self._generation = 0      # bumped by invalidate()
        self._result = None       # the last finished build's payload
        self._result_gen = -1     # the generation that build started in
        self._result_done = 0.0   # monotonic time it finished

    @property
    def enabled(self):
        return bool(self.instances)

    def invalidate(self):
        """Forget the cached payload. Never waits: a build already running is
        left to finish, but its result (which may predate whatever prompted
        this) is not reused as fresh."""
        with self._lock:
            self._generation += 1

    def _current(self):
        """The last result, if no invalidate() happened since its build started.
        Caller holds _lock."""
        if self._result is not None and self._result_gen == self._generation:
            return self._result
        return None

    def payload(self):
        if not self.enabled:
            return {"enabled": False}
        arrived = self._mono()
        with self._lock:
            arrived_gen = self._generation
            current = self._current()
            if current is not None and arrived - self._result_done < CACHE_TTL:
                return current
        if not self._build_lock.acquire(timeout=BUILD_WAIT):
            # Someone else's build is taking too long. Show what we had.
            with self._lock:
                if self._result is not None:
                    return self._result
            raise Busy()
        try:
            with self._lock:
                result = self._result
                # The build this viewer queued behind is its answer, however
                # long it took — unless it started before an invalidate() that
                # happened before this viewer arrived.
                joined = (result is not None and self._result_done >= arrived
                          and self._result_gen >= arrived_gen)
                fresh = (self._current() is not None
                         and self._mono() - self._result_done < CACHE_TTL)
                if joined or fresh:
                    return result
                generation = self._generation
            body = self._build(self._clock())
            with self._lock:
                self._result = body
                self._result_gen = generation
                self._result_done = self._mono()   # its age counts from when it landed
            return body
        finally:
            self._build_lock.release()

    def _fetch_all(self):
        def fetch(client):
            try:
                return client, client.torrents(), None
            except QbError as exc:
                return client, None, str(exc)

        with ThreadPoolExecutor(max_workers=max(1, len(self.clients))) as pool:
            return list(pool.map(fetch, self.clients))

    def _build(self, now):
        deadline = self._mono() + BUILD_DEADLINE
        sources, downloading, complete = [], [], []
        for client, torrents, error in self._fetch_all():
            label = client.instance.label
            if error is not None:
                sources.append({"label": label, "ok": False, "error": error})
                continue
            sources.append({"label": label, "ok": True})
            for torrent in torrents:
                if is_complete(torrent):
                    complete.append((client, torrent))
                else:
                    downloading.append(downloading_row(label, torrent))
        downloading.sort(key=_downloading_sort_key)

        scan = {"running": False, "percent": 0}
        ready, ready_status = [], "ok"
        try:
            task = jellyfin.library_task(self.jf_url, self.jf_api_key, get=self._jf_get)
            if jellyfin.task_busy(task):
                scan = {"running": True,
                        "percent": round(_num(task.get("CurrentProgressPercentage")), 1)}
            covered_until = coverage_point(task, now) - SKEW_SLACK
            candidates = []
            for client, torrent in complete:
                finished = completion_time(torrent)
                if finished is not None and finished > covered_until:
                    candidates.append((client, torrent, finished))
            if candidates:
                # Anchor on added_on, not completion: a library set to "use file
                # creation date" dates the item near when the download STARTED.
                earliest = min(_added_time(t, finished) for _c, t, finished in candidates)
                known = recent_path_components(
                    self.jf_url, self.jf_api_key, earliest - MATCH_WINDOW_PAD,
                    deadline, self._mono, get=self._jf_get)
                for client, torrent, finished in candidates:
                    # Past the deadline, no more per-torrent file listings.
                    if match_keys(client, torrent, list_files=self._mono() <= deadline) & known:
                        continue    # Jellyfin already has it
                    ready.append({
                        "source": client.instance.label,
                        "name": str(torrent.get("name") or ""),
                        "completed_at": int(finished),
                        "size": max(0, int(_num(torrent.get("size")))),
                        "adding": scan["running"],
                    })
        except JfError:
            ready, ready_status = [], "unknown"
        ready.sort(key=lambda row: -row["completed_at"])

        return {
            "enabled": True,
            "checked_at": int(now),
            "sources": sources,
            "downloading": downloading[:LIST_CAP],
            "downloading_total": len(downloading),
            "ready": ready[:LIST_CAP],
            "ready_status": ready_status,
            "scan": scan,
        }
