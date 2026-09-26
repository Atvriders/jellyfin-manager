# Incoming (qBittorrent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only "Incoming" panel that shows what qBittorrent (one or more boxes) is downloading and which finished downloads are not in Jellyfin yet, with a nudge on the Scan console telling people a scan will add them.

**Architecture:** A new pure-Python module `app/downloads.py` owns config parsing, a tiny read-only qBittorrent client, torrent classification, the "finished after the last completed library scan and not already in Jellyfin by name" rule, and a 10 s single-flight cache. `app/app.py` exposes it at `GET /api/downloads` and reuses its `RefreshLibrary` lookup for `/api/scan/progress`. The browser side is a self-contained `app/static/incoming.js` plus markup/CSS in `index.html`, coupled to the existing scan console only through two DOM events.

**Tech Stack:** Python 3.10+ (image runs 3.12), Flask 3, requests, pytest; vanilla JS; python-playwright + system Chrome for browser checks.

**Spec:** `docs/superpowers/specs/2026-09-26-qbittorrent-incoming-design.md` — read it first; this plan argues from it.

## Global Constraints

- **One commit at the end.** Do NOT commit per task (the owner's standing rule overrides the usual per-task commit step): stage nothing, commit nothing until the final verification task. Every task still ends green.
- Python code must run on **3.10** (local tests) and 3.12 (image). No `datetime.fromisoformat` on Jellyfin timestamps (7 fractional digits + `Z` fail on 3.10).
- **No new runtime dependencies.** `requirements.txt` is not touched by this plan.
- **Read-only toward qBittorrent:** the only calls are `POST /api/v2/auth/login`, `GET /api/v2/torrents/info`, `GET /api/v2/torrents/files`.
- **Unknown is never zero:** an unreachable box / Jellyfin is reported as such, never as "nothing downloading" / "nothing waiting".
- **Only fixed error categories leave the server** for qBittorrent sources: `unreachable`, `login failed`, `unexpected response`. Never raw exception text.
- **Field allow-list:** downloading rows `{source,name,progress,state,dlspeed,eta,size}`; ready rows `{source,name,completed_at,size,adding}`. No hash, path, tracker, category, or instance URL in any response.
- **Every server string is inserted with `textContent`**, never `innerHTML` (a test greps for it in `index.html` and `incoming.js`).
- **No real LAN addresses in the repo:** examples use RFC 5737 `192.0.2.x`.
- **Visual language:** reuse the existing tokens (`--lumen`, `--lantern`, `--coral`, `--benthos`, `--hairline*`, the three font variables). No new ornament; the jellyfish (`jellyfield.js`) is untouched.
- Tests run with `python3 -m pytest app/tests -q` from the repo root.

## Review Focus

The inputs the spec implies but the per-behaviour tests would not naturally hit; each has a test in the named task:

1. **A big seeding library** (thousands of long-finished torrents) must cost one `torrents/info` call per box and zero Jellyfin item listing / per-torrent file calls — `test_large_seeding_library_is_cheap` (Task 3).
2. **Jellyfin items with no `Path`** (virtual/missing episodes) or non-dict rows must be skipped, not crash the build — `test_items_without_a_path_are_ignored` (Task 3).
3. **A 240-character unbreakable torrent name** at 390 px must ellipsize with no horizontal page scroll — `test_hostile_widths_labels_and_future_times` (Task 6).
4. **Markup in an instance label** (`<b>Box</b>`) must render as literal text in the source tag — same test (Task 6).
5. **A box whose clock runs ahead** (`completed_at` in the future) must read "finished just now", not a negative age — same test (Task 6).

---

### Task 1: `downloads.py` — config, helpers, classification

**Files:**
- Create: `app/downloads.py` (first part — everything up to, not including, the `# ---- qBittorrent` section)
- Create: `app/tests/test_downloads.py` (preamble with all fakes + the config / classification / Jellyfin time + coverage sections)

**Interfaces:**
- Consumes: nothing new.
- Produces (used by Tasks 2–4):
  - constants `QB_TIMEOUT, JF_TIMEOUT, BUILD_DEADLINE, CACHE_TTL, SKEW_SLACK, FALLBACK_WINDOW, MATCH_WINDOW_PAD, ITEMS_PAGE, ITEMS_CAP, LIST_CAP, ETA_INFINITY, MAX_EPOCH, ERR_UNREACHABLE, ERR_LOGIN, ERR_BAD, UP_STATES, STATE_MAP, STATE_ORDER`
  - `class QbError(Exception)`, `class JfError(Exception)`
  - `@dataclass(frozen=True) Instance(label: str, url: str, username: str = "", password: str = "")`
  - `parse_instances(raw: str | None, warn: Callable[[str], None] | None = None) -> list[Instance]`
  - `_num(value, default=0.0) -> float`, `_nfc(text) -> str`, `_components(path) -> list[str]`, `_basename(path) -> str`
  - `parse_jf_time(value) -> float | None` (epoch seconds)
  - `is_complete(torrent: dict) -> bool`, `display_state(torrent: dict) -> str`, `completion_time(torrent: dict) -> float | None`, `_added_time(torrent: dict, finished: float) -> float`, `downloading_row(source: str, torrent: dict) -> dict`, `_downloading_sort_key(row: dict)`, `coverage_point(task: dict | None, now: float) -> float`

- [ ] **Step 1: Write the failing tests.** Create `app/tests/test_downloads.py` with exactly this content (the fakes at the top are used by Tasks 2–4 too):

```python
"""The Incoming panel: qBittorrent downloads + "finished but not in Jellyfin yet".

Everything runs against scripted fakes: FakeQb stands in for a qBittorrent
WebUI (requests.Session-shaped), FakeJellyfin for requests.get against
Jellyfin. No network.
"""

import json

import pytest
import requests

import app as app_module
import downloads as dl
from conftest import FakeResponse

NOW = 1_790_000_000.0          # fixed "now" for every Incoming built here
LAST_SCAN_START = NOW - 3600   # the last completed library scan started an hour ago


def iso(epoch):
    """Epoch seconds -> Jellyfin-style timestamp (7 fractional digits + Z)."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeQb:
    """A scripted qBittorrent WebUI, shaped like requests.Session."""

    def __init__(self, torrents=None, files=None, password=None, fail=None):
        self.torrents = torrents if torrents is not None else []
        self.files = files or {}
        self.password = password       # None -> subnet auth bypass, no login needed
        self.fail = fail               # exception raised by every call
        self.logged_in = False
        self.expire_next = False       # the next GET answers 403 once (session expired)
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data, headers))
        if self.fail:
            raise self.fail
        if self.password is not None and data.get("password") == self.password:
            self.logged_in = True
            return Resp(text="Ok.")
        return Resp(text="Fails.")

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        if self.fail:
            raise self.fail
        if self.expire_next:
            self.expire_next = False
            self.logged_in = False
            return Resp(status=403)
        if self.password is not None and not self.logged_in:
            return Resp(status=403)
        if url.endswith("/api/v2/torrents/info"):
            return Resp(data=self.torrents)
        if url.endswith("/api/v2/torrents/files"):
            return Resp(data=self.files.get(params["hash"], []))
        return Resp(status=404)


class FakeJellyfin:
    """Scripted Jellyfin: /ScheduledTasks and a paged /Items (newest first)."""

    def __init__(self, items=None, status="Completed", state="Idle", start=LAST_SCAN_START, fail=None):
        self.items = items or []      # [{"Path":..., "DateCreated":...}], newest first
        self.task = {
            "Name": "Scan Media Library", "Key": "RefreshLibrary", "State": state,
            "CurrentProgressPercentage": 37.25 if state == "Running" else None,
            "LastExecutionResult": {"StartTimeUtc": iso(start), "EndTimeUtc": iso(start + 60),
                                    "Status": status},
        }
        self.fail = fail
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        if self.fail:
            raise self.fail
        if url.endswith("/ScheduledTasks"):
            return Resp(data=[{"Name": "Refresh Guide", "Key": "RefreshGuide", "State": "Running"}, self.task])
        if url.endswith("/Items"):
            start, limit = int(params["StartIndex"]), int(params["Limit"])
            return Resp(data={"Items": self.items[start:start + limit]})
        return Resp(status=404)

    @property
    def item_calls(self):
        return [c for c in self.calls if c[0].endswith("/Items")]


def torrent(name, **kw):
    """A qBittorrent /torrents/info row. Defaults: complete, saved into /movies."""
    row = {
        "hash": "h-" + name, "name": name, "state": "stalledUP", "progress": 1,
        "dlspeed": 0, "eta": 8640000, "size": 1000, "amount_left": 0,
        "added_on": int(NOW - 1800), "completion_on": int(NOW - 600),
        "save_path": "/movies", "content_path": "/movies/" + name,
    }
    row.update(kw)
    return row


def build(qbs, jellyfin=None, clock=None):
    """An Incoming over the given {label: FakeQb} with a fixed clock."""
    fakes = list(qbs.values())
    instances = [dl.Instance(label, f"http://qb{i}.test:8080") for i, label in enumerate(qbs)]
    jellyfin = jellyfin if jellyfin is not None else FakeJellyfin()
    return dl.Incoming(
        instances, "http://jellyfin.test", "api-key",
        session_factory=lambda it=iter(fakes): next(it),
        jf_get=jellyfin.get, clock=clock or (lambda: NOW),
    )


# ------------------------------------------------------------------ config


def test_parse_instances_two_labelled_urls():
    got = dl.parse_instances(" Box 1 = http://192.0.2.10:8080/ , Box 2=https://qb.example.test ")
    assert got == [dl.Instance("Box 1", "http://192.0.2.10:8080"),
                   dl.Instance("Box 2", "https://qb.example.test")]


def test_parse_instances_strips_and_decodes_credentials():
    (inst,) = dl.parse_instances("Box=http://admin:p%40ss%2Cword@192.0.2.10:8080")
    assert inst.url == "http://192.0.2.10:8080"
    assert (inst.username, inst.password) == ("admin", "p@ss,word")


def test_parse_instances_keeps_ipv6_brackets():
    (inst,) = dl.parse_instances("Box=http://[2001:db8::1]:8080")
    assert inst.url == "http://[2001:db8::1]:8080"


def test_parse_instances_skips_bad_entries_and_never_logs_the_url():
    warnings = []
    got = dl.parse_instances(
        "nourl, =http://x.test, Box=ftp://u:hunter2@x.test, Ok=http://192.0.2.1:8080, Ok=http://192.0.2.2",
        warn=warnings.append)
    assert [i.label for i in got] == ["Ok"]
    assert got[0].url == "http://192.0.2.1:8080"
    assert len(warnings) == 4
    assert not any("hunter2" in w or "x.test" in w for w in warnings)


@pytest.mark.parametrize("raw", [None, "", "  ", " , "])
def test_parse_instances_empty_means_disabled(raw):
    assert dl.parse_instances(raw) == []


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize("state,expected", [
    ("downloading", "downloading"), ("forcedDL", "downloading"),
    ("metaDL", "metadata"), ("forcedMetaDL", "metadata"),
    ("stalledDL", "stalled"), ("queuedDL", "queued"),
    ("checkingDL", "checking"), ("checkingResumeData", "checking"),
    ("allocating", "checking"), ("moving", "checking"),
    ("pausedDL", "paused"), ("stoppedDL", "paused"),
    ("error", "error"), ("missingFiles", "error"),
])
def test_display_state_table(state, expected):
    assert dl.display_state({"state": state, "amount_left": 5}) == expected


def test_negative_amount_left_is_stuck_whatever_the_state():
    assert dl.display_state({"state": "stalledDL", "amount_left": -4096}) == "stuck"
    assert dl.display_state({"state": "downloading", "amount_left": -1}) == "stuck"


def test_unknown_state_falls_back_on_speed():
    assert dl.display_state({"state": "someNewState", "dlspeed": 10}) == "downloading"
    assert dl.display_state({"state": None, "dlspeed": 0}) == "stalled"


@pytest.mark.parametrize("row,complete", [
    ({"progress": 1, "state": "stalledDL"}, True),
    ({"progress": 0.2, "state": "pausedUP"}, True),
    ({"progress": 0.2, "state": "stoppedUP"}, True),
    ({"progress": 0.999, "state": "downloading"}, False),
    ({"progress": "garbage", "state": "downloading"}, False),
])
def test_is_complete(row, complete):
    assert dl.is_complete(row) is complete


@pytest.mark.parametrize("value,expected", [
    (1_700_000_000, 1_700_000_000), (0, None), (-1, None), (None, None),
    ("x", None), (4_294_967_295, None), (float("nan"), None),
])
def test_completion_time_rejects_garbage(value, expected):
    assert dl.completion_time({"completion_on": value}) == expected


def test_downloading_row_normalizes_and_allowlists():
    row = dl.downloading_row("Box", {
        "name": "A", "progress": 1.7, "state": "downloading", "dlspeed": -3,
        "eta": 8640000, "size": "12", "hash": "secret", "save_path": "/movies",
    })
    assert row == {"source": "Box", "name": "A", "progress": 1.0, "state": "downloading",
                   "dlspeed": 0, "eta": None, "size": 12}


# ------------------------------------------------------------------ Jellyfin time + coverage


def test_parse_jf_time_handles_seven_digit_fraction_and_z():
    assert dl.parse_jf_time("2026-09-26T14:03:11.1234567Z") == pytest.approx(1790431391.123456)


def test_parse_jf_time_offsets_and_naive():
    assert dl.parse_jf_time("2026-09-26T16:03:11+02:00") == dl.parse_jf_time("2026-09-26T14:03:11Z")
    assert dl.parse_jf_time("2026-09-26T14:03:11") == dl.parse_jf_time("2026-09-26T14:03:11Z")


@pytest.mark.parametrize("bad", [None, "", "yesterday", "2026-13-40T99:99:99Z", 12345])
def test_parse_jf_time_garbage_is_none(bad):
    assert dl.parse_jf_time(bad) is None


def test_coverage_point_trusts_only_completed_runs():
    task = FakeJellyfin().task
    assert dl.coverage_point(task, NOW) == pytest.approx(LAST_SCAN_START, abs=1)
    for status in ("Failed", "Cancelled", "Aborted"):
        task["LastExecutionResult"]["Status"] = status
        assert dl.coverage_point(task, NOW) == NOW - dl.FALLBACK_WINDOW
    assert dl.coverage_point(None, NOW) == NOW - dl.FALLBACK_WINDOW
    assert dl.coverage_point({"LastExecutionResult": None}, NOW) == NOW - dl.FALLBACK_WINDOW
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_downloads.py -q`
Expected: collection error / FAIL with `ModuleNotFoundError: No module named 'downloads'`.

- [ ] **Step 3: Implement.** Create `app/downloads.py` with exactly this content:

```python
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
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

import requests

QB_TIMEOUT = 5              # seconds, per qBittorrent request
JF_TIMEOUT = 5              # seconds, per Jellyfin request
BUILD_DEADLINE = 12         # seconds; Jellyfin paging stops (-> "unknown") past this
CACHE_TTL = 10              # seconds the whole payload is reused across viewers
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


class JfError(Exception):
    """Jellyfin could not answer (unreachable, non-200, or garbage)."""


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


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_jf_time(value):
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
    if tz in (None, "Z"):
        tzinfo = timezone.utc
    else:
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        tzinfo = timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                        micro, tzinfo=tzinfo).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- classification


def is_complete(torrent):
    return _num(torrent.get("progress")) >= 1 or torrent.get("state") in UP_STATES


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
        start = parse_jf_time(last.get("StartTimeUtc"))
        if start is not None:
            return start
    return now - FALLBACK_WINDOW
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_downloads.py -q`
Expected: all tests in the file PASS. Then `python3 -m pytest app/tests -q` — the original 110 still pass.

---

### Task 2: `downloads.py` — qBittorrent client, match keys, Jellyfin helpers

**Files:**
- Modify: `app/downloads.py` (append the `qBittorrent` and `Jellyfin` sections)
- Modify: `app/tests/test_downloads.py` (append the `qBittorrent client` and `match keys` sections)

**Interfaces:**
- Consumes: everything Task 1 produces.
- Produces (used by Tasks 3–4):
  - `class QbClient(instance: Instance, session=None)` with `.call(path, params=None) -> Any` (JSON), `.torrents() -> list[dict]`, `.file_names(torrent_hash) -> list[str]`; raises `QbError` whose `str()` is one of the `ERR_*` categories.
  - `match_keys(client: QbClient, torrent: dict) -> set[str]` (NFC-normalized)
  - `jf_get_json(jf_url, api_key, path, params=None, get=None) -> Any` — raises `JfError` on anything but a 200 JSON answer (including unconfigured URL/key).
  - `fetch_library_task(jf_url, api_key, get=None) -> dict | None` — the task with `Key == "RefreshLibrary"`.
  - `recent_path_components(jf_url, api_key, window_start, deadline, clock, get=None) -> set[str]`

- [ ] **Step 1: Write the failing tests.** Append to `app/tests/test_downloads.py`:

```python
# ------------------------------------------------------------------ qBittorrent client


def client_for(fake, username="", password=""):
    return dl.QbClient(dl.Instance("Box", "http://qb.test:8080", username, password), fake)


def test_no_credentials_means_no_login_call():
    fake = FakeQb(torrents=[torrent("A")])
    assert [t["name"] for t in client_for(fake).torrents()] == ["A"]
    assert [c[0] for c in fake.calls] == ["GET"]


def test_every_request_sends_the_referer():
    fake = FakeQb(password="pw")
    client_for(fake, "admin", "pw").torrents()
    assert all(c[3] == {"Referer": "http://qb.test:8080"} for c in fake.calls)


def test_credentials_log_in_first_and_password_only_in_the_login_body():
    fake = FakeQb(torrents=[torrent("A")], password="pw")
    client_for(fake, "admin", "pw").torrents()
    assert [c[0] for c in fake.calls] == ["POST", "GET"]
    assert fake.calls[0][2] == {"username": "admin", "password": "pw"}
    assert "pw" not in json.dumps([c[1:3] for c in fake.calls[1:]])


def test_expired_session_relogs_in_once():
    fake = FakeQb(torrents=[torrent("A")], password="pw")
    client = client_for(fake, "admin", "pw")
    client.torrents()
    fake.expire_next = True
    assert [t["name"] for t in client.torrents()] == ["A"]
    assert [c[0] for c in fake.calls] == ["POST", "GET", "GET", "POST", "GET"]


def test_wrong_password_is_login_failed():
    with pytest.raises(dl.QbError, match="^login failed$"):
        client_for(FakeQb(password="pw"), "admin", "nope").torrents()


def test_403_without_credentials_is_login_failed():
    with pytest.raises(dl.QbError, match="^login failed$"):
        client_for(FakeQb(password="pw")).torrents()


def test_403_even_after_relogin_is_login_failed():
    class AlwaysForbidden(FakeQb):
        def get(self, url, params=None, headers=None, timeout=None):
            self.calls.append(("GET", url, params, headers))
            return Resp(status=403)
    with pytest.raises(dl.QbError, match="^login failed$"):
        client_for(AlwaysForbidden(password="pw"), "admin", "pw").torrents()


@pytest.mark.parametrize("fake,category", [
    (FakeQb(fail=requests.ConnectionError("http://admin:pw@qb.test refused")), "unreachable"),
    (FakeQb(fail=requests.Timeout()), "unreachable"),
    (FakeQb(torrents=ValueError("not json")), "unexpected response"),
    (FakeQb(torrents={"not": "a list"}), "unexpected response"),
])
def test_failures_become_fixed_categories(fake, category):
    with pytest.raises(dl.QbError) as info:
        client_for(fake).torrents()
    assert str(info.value) == category


def test_http_500_is_unexpected_response():
    class Broken(FakeQb):
        def get(self, url, params=None, headers=None, timeout=None):
            return Resp(status=500)
    with pytest.raises(dl.QbError, match="^unexpected response$"):
        client_for(Broken()).torrents()


# ------------------------------------------------------------------ match keys


def test_match_keys_folder_torrent_uses_content_basename_and_name():
    t = torrent("Dune.2021.1080p", content_path="/movies/Dune (2021)")
    assert dl.match_keys(client_for(FakeQb()), t) == {"Dune (2021)", "Dune.2021.1080p"}


def test_match_keys_no_root_folder_uses_file_names():
    fake = FakeQb(files={"h-Pack": [{"name": "Show S01E01.mkv"}, {"name": "sub/Show S01E02.mkv"}]})
    t = torrent("Pack", content_path="/tv/Show/", save_path="/tv/Show")
    assert dl.match_keys(client_for(fake), t) == {"Show S01E01.mkv", "Show S01E02.mkv"}


def test_match_keys_files_failure_yields_no_keys():
    t = torrent("Pack", content_path="/tv/Show", save_path="/tv/Show")
    assert dl.match_keys(client_for(FakeQb(fail=requests.ConnectionError())), t) == set()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_downloads.py -q`
Expected: the new tests FAIL with `AttributeError: module 'downloads' has no attribute 'QbClient'` (Task 1 tests still pass).

- [ ] **Step 3: Implement.** Append to `app/downloads.py`:

```python
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
        if resp.status_code != 200 or (resp.text or "").strip() != "Ok.":
            raise QbError(ERR_LOGIN)
        self._logged_in = True

    def _get(self, path, params):
        return self.session.get(self.instance.url + path, params=params,
                                headers=self._headers(), timeout=QB_TIMEOUT)

    def call(self, path, params=None):
        """GET a JSON endpoint. A 403 means the session expired: log in again once."""
        try:
            if self.instance.username and not self._logged_in:
                self._login()
            resp = self._get(path, params)
            if resp.status_code == 403:
                if not self.instance.username:
                    raise QbError(ERR_LOGIN)   # no credentials to retry with
                self._logged_in = False
                self._login()
                resp = self._get(path, params)
                if resp.status_code == 403:
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


def match_keys(client, torrent):
    """Names under which this torrent's content would appear in a Jellyfin path.

    Normally the basename of content_path (plus `name` if it differs). When
    content_path == save_path the torrent has no root folder: its basename would
    be the library folder itself and match everything, so use the file names.
    """
    content = str(torrent.get("content_path") or "")
    save = str(torrent.get("save_path") or "")
    keys = set()
    if content and content.rstrip("\\/") == save.rstrip("\\/"):
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


def jf_get_json(jf_url, api_key, path, params=None, get=None):
    """GET a Jellyfin JSON endpoint; every failure becomes JfError."""
    if not jf_url or not api_key:
        raise JfError("Jellyfin is not configured")
    getter = get or requests.get
    try:
        resp = getter(
            jf_url + path,
            params=params,
            headers={"X-Emby-Token": api_key, "Accept": "application/json"},
            timeout=JF_TIMEOUT,
        )
        if resp.status_code != 200:
            raise JfError(f"HTTP {resp.status_code}")
        return resp.json()
    except JfError:
        raise
    except Exception as exc:
        raise JfError("Jellyfin request failed") from exc


def fetch_library_task(jf_url, api_key, get=None):
    """The "Scan Media Library" scheduled task (Key == "RefreshLibrary"), or None."""
    tasks = jf_get_json(jf_url, api_key, "/ScheduledTasks", get=get)
    if not isinstance(tasks, list):
        raise JfError("unexpected ScheduledTasks response")
    for task in tasks:
        if isinstance(task, dict) and task.get("Key") == "RefreshLibrary":
            return task
    return None


def recent_path_components(jf_url, api_key, window_start, deadline, clock, get=None):
    """Every path component of items Jellyfin added since window_start (newest first)."""
    components = set()
    start = 0
    while start < ITEMS_CAP:
        if clock() > deadline:
            raise JfError("Jellyfin item listing took too long")
        data = jf_get_json(jf_url, api_key, "/Items", params={
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
        oldest = parse_jf_time(batch[-1].get("DateCreated") if isinstance(batch[-1], dict) else None)
        if oldest is not None and oldest < window_start:
            break
        start += ITEMS_PAGE
    return components
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_downloads.py -q`
Expected: PASS.

---

### Task 3: `downloads.py` — the `Incoming` payload, ready rule, cache

**Files:**
- Modify: `app/downloads.py` (append the `the panel` section)
- Modify: `app/tests/test_downloads.py` (append the `Incoming payload` section)

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces (used by Task 4):
  - `class Incoming(instances, jf_url="", jf_api_key="", session_factory=None, jf_get=None, clock=None)`
  - `.enabled -> bool`, `.payload() -> dict` (the spec's `/api/downloads` shape, or `{"enabled": False}`), `.invalidate() -> None`

- [ ] **Step 1: Write the failing tests.** Append to `app/tests/test_downloads.py`:

```python
# ------------------------------------------------------------------ Incoming payload


def test_disabled_without_instances():
    assert dl.Incoming([]).payload() == {"enabled": False}
    assert dl.Incoming([]).enabled is False


def test_downloading_rows_sorted_and_capped():
    rows = [torrent(f"p{i}", state="pausedDL", progress=0.5, completion_on=-1) for i in range(30)]
    rows.append(torrent("live", state="downloading", progress=0.1, dlspeed=500, eta=60, completion_on=-1))
    rows.append(torrent("stuck", state="stalledDL", progress=0.99, amount_left=-10, completion_on=-1))
    body = build({"Box": FakeQb(torrents=rows)}).payload()
    assert body["downloading_total"] == 32
    assert len(body["downloading"]) == dl.LIST_CAP
    assert [r["name"] for r in body["downloading"][:2]] == ["live", "stuck"]
    assert body["downloading"][0] == {"source": "Box", "name": "live", "progress": 0.1,
                                      "state": "downloading", "dlspeed": 500, "eta": 60, "size": 1000}


def test_finished_after_last_scan_and_unknown_to_jellyfin_is_ready():
    qb = FakeQb(torrents=[torrent("New.Movie.2026", completion_on=int(NOW - 600), size=42)])
    body = build({"Box": qb}).payload()
    assert body["ready_status"] == "ok"
    assert body["ready"] == [{"source": "Box", "name": "New.Movie.2026",
                              "completed_at": int(NOW - 600), "size": 42, "adding": False}]


def test_finished_before_last_scan_is_not_ready():
    qb = FakeQb(torrents=[torrent("Old", completion_on=int(LAST_SCAN_START - 3600))])
    jf = FakeJellyfin()
    assert build({"Box": qb}, jf).payload()["ready"] == []
    assert jf.item_calls == []      # no candidates -> Jellyfin items never listed


def test_skew_slack_errs_toward_showing():
    qb = FakeQb(torrents=[torrent("Edge", completion_on=int(LAST_SCAN_START - 60))])
    assert [r["name"] for r in build({"Box": qb}).payload()["ready"]] == ["Edge"]


def test_untrusted_last_scan_falls_back_to_48h_window():
    qb = FakeQb(torrents=[
        torrent("Day old", completion_on=int(NOW - 24 * 3600), added_on=int(NOW - 25 * 3600)),
        torrent("Week old", completion_on=int(NOW - 7 * 24 * 3600), added_on=int(NOW - 8 * 24 * 3600)),
    ])
    body = build({"Box": qb}, FakeJellyfin(status="Failed")).payload()
    assert [r["name"] for r in body["ready"]] == ["Day old"]


def test_running_scan_marks_ready_rows_adding():
    qb = FakeQb(torrents=[torrent("New")])
    body = build({"Box": qb}, FakeJellyfin(state="Running")).payload()
    assert body["scan"] == {"running": True, "percent": 37.2}
    assert body["ready"][0]["adding"] is True


@pytest.mark.parametrize("item_path", [
    "/data/movies/New.Movie.2026/New.Movie.2026.mkv",   # folder torrent, different mount prefix
    "D:\\media\\movies\\New.Movie.2026\\movie.mkv",       # backslash paths
])
def test_jellyfin_already_has_it_by_folder_name(item_path):
    qb = FakeQb(torrents=[torrent("New.Movie.2026")])
    jf = FakeJellyfin(items=[{"Path": item_path, "DateCreated": iso(NOW - 300)}])
    assert build({"Box": qb}, jf).payload()["ready"] == []


def test_jellyfin_already_has_single_file_torrent():
    qb = FakeQb(torrents=[torrent("Film.mkv", content_path="/movies/Film.mkv")])
    jf = FakeJellyfin(items=[{"Path": "/media/movies/Film.mkv", "DateCreated": iso(NOW - 300)}])
    assert build({"Box": qb}, jf).payload()["ready"] == []


def test_match_is_unicode_normalized():
    decomposed = "Ame\u0301lie"          # e + combining acute
    composed = "Am\u00e9lie"             # precomposed é
    qb = FakeQb(torrents=[torrent(decomposed)])
    jf = FakeJellyfin(items=[{"Path": f"/m/{composed}/x.mkv", "DateCreated": iso(NOW - 300)}])
    assert build({"Box": qb}, jf).payload()["ready"] == []


def test_match_window_is_anchored_on_added_on_and_pages():
    # 450 unrelated recent items, then the match: its DateCreated is near when
    # the download STARTED (library uses file creation date), so paging must
    # continue past items older than the completion time.
    filler = [{"Path": f"/tv/Other/e{i}.mkv", "DateCreated": iso(NOW - 60 - i)} for i in range(450)]
    match = {"Path": "/movies/Slow.Download/slow.mkv", "DateCreated": iso(NOW - 5 * 3600)}
    qb = FakeQb(torrents=[torrent("Slow.Download", added_on=int(NOW - 5 * 3600 - 30),
                                  completion_on=int(NOW - 600))])
    jf = FakeJellyfin(items=filler + [match])
    assert build({"Box": qb}, jf).payload()["ready"] == []
    assert len(jf.item_calls) == 3      # pages of 200: 0, 200, 400


def test_paging_stops_once_items_are_older_than_the_window():
    old = [{"Path": f"/tv/Old/e{i}.mkv", "DateCreated": iso(NOW - 30 * 24 * 3600 - i)} for i in range(1000)]
    jf = FakeJellyfin(items=old)
    build({"Box": FakeQb(torrents=[torrent("New")])}, jf).payload()
    assert len(jf.item_calls) == 1


def test_item_listing_is_capped():
    items = [{"Path": f"/tv/X/e{i}.mkv", "DateCreated": iso(NOW - 10)} for i in range(5000)]
    jf = FakeJellyfin(items=items)
    body = build({"Box": FakeQb(torrents=[torrent("New")])}, jf).payload()
    assert len(jf.item_calls) == dl.ITEMS_CAP // dl.ITEMS_PAGE
    assert [r["name"] for r in body["ready"]] == ["New"]


def test_slow_jellyfin_past_the_deadline_is_unknown():
    ticks = iter([NOW] + [NOW + dl.BUILD_DEADLINE + 1] * 50)
    items = [{"Path": f"/tv/X/e{i}.mkv", "DateCreated": iso(NOW - 10)} for i in range(400)]
    body = build({"Box": FakeQb(torrents=[torrent("New")])}, FakeJellyfin(items=items),
                 clock=lambda: next(ticks)).payload()
    assert body["ready_status"] == "unknown" and body["ready"] == []


def test_large_seeding_library_is_cheap():
    # 5,000 long-finished seeding torrents: none are candidates, so no per-torrent
    # file listing and no Jellyfin item listing happen at all.
    qb = FakeQb(torrents=[torrent(f"seed{i}", completion_on=int(NOW - 90 * 24 * 3600))
                          for i in range(5000)])
    jf = FakeJellyfin()
    body = build({"Box": qb}, jf).payload()
    assert body["ready"] == [] and body["downloading_total"] == 0
    assert [c[1] for c in qb.calls] == ["http://qb0.test:8080/api/v2/torrents/info"]
    assert jf.item_calls == []


def test_items_without_a_path_are_ignored():
    jf = FakeJellyfin(items=[{"Path": None, "DateCreated": iso(NOW - 5)}, {"DateCreated": iso(NOW - 6)},
                             "not-a-dict"])
    body = build({"Box": FakeQb(torrents=[torrent("New")])}, jf).payload()
    assert [r["name"] for r in body["ready"]] == ["New"]


def test_ready_sorted_newest_first():
    qb = FakeQb(torrents=[torrent("older", completion_on=int(NOW - 900)),
                          torrent("newer", completion_on=int(NOW - 100))])
    assert [r["name"] for r in build({"Box": qb}).payload()["ready"]] == ["newer", "older"]


def test_one_box_down_does_not_hide_the_other():
    body = build({
        "Box 1": FakeQb(torrents=[torrent("A", state="downloading", progress=0.5, completion_on=-1)]),
        "Box 2": FakeQb(fail=requests.ConnectionError("http://admin:pw@192.0.2.11 refused")),
    }).payload()
    assert body["sources"] == [{"label": "Box 1", "ok": True},
                               {"label": "Box 2", "ok": False, "error": "unreachable"}]
    assert [r["name"] for r in body["downloading"]] == ["A"]
    assert "pw" not in json.dumps(body)


def test_jellyfin_down_is_unknown_not_zero():
    body = build({"Box": FakeQb(torrents=[torrent("New")])},
                 FakeJellyfin(fail=requests.ConnectionError())).payload()
    assert body["ready_status"] == "unknown"
    assert body["ready"] == []
    assert body["sources"] == [{"label": "Box", "ok": True}]


def test_jellyfin_unconfigured_is_unknown():
    incoming = dl.Incoming([dl.Instance("Box", "http://qb.test")], "", "",
                           session_factory=lambda: FakeQb(torrents=[torrent("New")]),
                           clock=lambda: NOW)
    assert incoming.payload()["ready_status"] == "unknown"


def test_payload_is_cached_then_refreshed_and_invalidated():
    now = [NOW]
    qb = FakeQb(torrents=[])
    incoming = build({"Box": qb}, clock=lambda: now[0])
    incoming.payload()
    incoming.payload()
    assert len(qb.calls) == 1                        # second call served from cache
    now[0] += dl.CACHE_TTL
    incoming.payload()
    assert len(qb.calls) == 2                        # TTL expired
    incoming.invalidate()
    incoming.payload()
    assert len(qb.calls) == 3                        # invalidated


def test_payload_never_leaks_hashes_paths_or_urls():
    qb = FakeQb(torrents=[
        torrent("R", tracker="http://tracker.test/announce?passkey=SECRET", category="movies"),
        torrent("D", state="downloading", progress=0.3, completion_on=-1),
    ])
    body = build({"Box": qb}).payload()
    text = json.dumps(body)
    for leaked in ("h-R", "h-D", "/movies", "qb0.test", "SECRET", "tracker", "category"):
        assert leaked not in text
    assert set(body) == {"enabled", "checked_at", "sources", "downloading", "downloading_total",
                         "ready", "ready_status", "scan"}
    assert set(body["downloading"][0]) == {"source", "name", "progress", "state", "dlspeed", "eta", "size"}
    assert set(body["ready"][0]) == {"source", "name", "completed_at", "size", "adding"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_downloads.py -q`
Expected: the new tests FAIL with `AttributeError: module 'downloads' has no attribute 'Incoming'`.

- [ ] **Step 3: Implement.** Append to `app/downloads.py`:

```python
# ---------------------------------------------------------------- the panel


class Incoming:
    """Builds (and caches) the /api/downloads payload."""

    def __init__(self, instances, jf_url="", jf_api_key="", session_factory=None,
                 jf_get=None, clock=None):
        self.instances = list(instances)
        make_session = session_factory or requests.Session
        self.clients = [QbClient(inst, make_session()) for inst in self.instances]
        self.jf_url = (jf_url or "").rstrip("/")
        self.jf_api_key = jf_api_key or ""
        self._jf_get = jf_get
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._cache = None
        self._cache_at = 0.0

    @property
    def enabled(self):
        return bool(self.instances)

    def invalidate(self):
        with self._lock:
            self._cache = None

    def payload(self):
        if not self.enabled:
            return {"enabled": False}
        # Single-flight: concurrent viewers wait here and then reuse the result,
        # so N open tabs cost one upstream round per CACHE_TTL.
        with self._lock:
            now = self._clock()
            if self._cache is not None and now - self._cache_at < CACHE_TTL:
                return self._cache
            self._cache = self._build(now)
            self._cache_at = now
            return self._cache

    def _fetch_all(self):
        def fetch(client):
            try:
                return client, client.torrents(), None
            except QbError as exc:
                return client, None, str(exc)

        with ThreadPoolExecutor(max_workers=max(1, len(self.clients))) as pool:
            return list(pool.map(fetch, self.clients))

    def _build(self, now):
        deadline = now + BUILD_DEADLINE
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
            task = fetch_library_task(self.jf_url, self.jf_api_key, get=self._jf_get)
            if task and task.get("State") == "Running":
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
                    deadline, self._clock, get=self._jf_get)
                for client, torrent, finished in candidates:
                    if match_keys(client, torrent) & known:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_downloads.py -q`
Expected: PASS.

---

### Task 4: Flask wiring — `/api/downloads`, `RefreshLibrary` progress, cache invalidation

**Files:**
- Modify: `app/app.py` (import, module-level `incoming`, `index()`, `scan_progress()`, new `api_downloads()`, `scan()`)
- Modify: `app/tests/test_app.py` (the three `/api/scan/progress` tests)
- Modify: `app/tests/test_downloads.py` (append the `Flask wiring` section)

**Interfaces:**
- Consumes: `downloads.Incoming`, `downloads.parse_instances`, `downloads.fetch_library_task`, `downloads.JfError`, `downloads._num`.
- Produces: module attribute `app.incoming` (tests monkeypatch it); template variable `incoming_enabled` (Task 5 uses it); `GET /api/downloads`.

- [ ] **Step 1: Write the failing tests.** Append to `app/tests/test_downloads.py`:

```python
# ------------------------------------------------------------------ Flask wiring


class StubIncoming:
    def __init__(self, body):
        self.body = body
        self.invalidated = 0

    @property
    def enabled(self):
        return self.body.get("enabled", False)

    def payload(self):
        return self.body

    def invalidate(self):
        self.invalidated += 1


def test_downloads_endpoint_requires_auth(client):
    r = client.get("/api/downloads")
    assert r.status_code == 401
    assert r.get_json() == {"error": "Unauthorized"}


def test_downloads_endpoint_disabled(auth, monkeypatch):
    monkeypatch.setattr(app_module, "incoming", dl.Incoming([]))
    assert auth.get("/api/downloads").get_json() == {"enabled": False}


def test_downloads_endpoint_returns_payload(auth, monkeypatch):
    body = {"enabled": True, "sources": [], "downloading": [], "downloading_total": 0,
            "ready": [], "ready_status": "ok", "scan": {"running": False, "percent": 0},
            "checked_at": 1}
    monkeypatch.setattr(app_module, "incoming", StubIncoming(body))
    assert auth.get("/api/downloads").get_json() == body


def test_downloads_endpoint_unexpected_crash_is_generic_500(auth, monkeypatch):
    class Boom(StubIncoming):
        def payload(self):
            raise RuntimeError("http://admin:pw@qb.test exploded")
    monkeypatch.setattr(app_module, "incoming", Boom({"enabled": True}))
    r = auth.get("/api/downloads")
    assert r.status_code == 500
    assert "pw" not in r.get_data(as_text=True)


def test_successful_scan_invalidates_the_downloads_cache(auth, monkeypatch):
    stub = StubIncoming({"enabled": True})
    monkeypatch.setattr(app_module, "incoming", stub)
    monkeypatch.setattr(app_module.requests, "post", lambda *a, **kw: FakeResponse())
    assert auth.post("/api/scan").status_code == 200
    assert stub.invalidated == 1


def test_index_renders_incoming_panel_only_when_enabled(auth, monkeypatch):
    monkeypatch.setattr(app_module, "incoming", dl.Incoming([]))
    assert 'id="incoming"' not in auth.get("/").get_data(as_text=True)
    monkeypatch.setattr(app_module, "incoming", StubIncoming({"enabled": True}))
    page = auth.get("/").get_data(as_text=True)
    assert 'id="incoming"' in page
    assert 'id="incoming-nudge"' in page
    assert "incoming.js" in page
```

Then update the progress tests in `app/tests/test_app.py`. Replace the section header comment `# --- GET /api/scan/progress (must stay untouched) -------------------------` with `# --- GET /api/scan/progress ---------------------------------------------------`, and:

In `test_progress_reports_running_task`, replace the line
`        {"Name": "Scan Media Library", "State": "Running", "CurrentProgressPercentage": 42.345},`
with
```python
        {"Name": "Refresh Guide", "Key": "RefreshGuide", "State": "Running", "CurrentProgressPercentage": 77},
        {"Name": "Scan Media Library", "Key": "RefreshLibrary", "State": "Running", "CurrentProgressPercentage": 42.345},
```

In `test_progress_reports_idle`, replace
`    tasks = [{"Name": "Scan Media Library", "State": "Idle", "CurrentProgressPercentage": 0}]`
with
```python
    tasks = [
        {"Name": "Refresh Guide", "Key": "RefreshGuide", "State": "Running", "CurrentProgressPercentage": 5},
        {"Name": "Scan Media Library", "Key": "RefreshLibrary", "State": "Idle", "CurrentProgressPercentage": None},
    ]
```

Replace the whole `test_progress_errors_are_500` function with:
```python
def test_progress_errors_are_502_without_raw_exception_text(auth, monkeypatch):
    def fake_get(*a, **kw):
        raise RuntimeError("http://jellyfin.test/ScheduledTasks?api_key=api-key refused")

    monkeypatch.setattr(app_module.requests, "get", fake_get)

    r = auth.get("/api/scan/progress")
    assert r.status_code == 502
    assert r.get_json() == {"error": "Couldn't reach Jellyfin"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests -q`
Expected: FAIL — `/api/downloads` is 404, `app_module.incoming` doesn't exist, the idle test reports `running` (Refresh Guide matched by keyword), the error test gets 500 with raw text.

- [ ] **Step 3: Implement.** In `app/app.py`:

(a) Add `import downloads` directly above `from history import ...`.

(b) Directly above `def jf_headers():`, add:
```python
# The "Incoming" panel (qBittorrent downloads + finished-but-not-in-Jellyfin).
# Off unless QBITTORRENT_INSTANCES is set. Read-only: never touches a torrent.
incoming = downloads.Incoming(
    downloads.parse_instances(os.environ.get("QBITTORRENT_INSTANCES", ""), warn=app.logger.warning),
    JELLYFIN_URL,
    JELLYFIN_API_KEY,
)
```

(c) In `index()`, change `return render_template("index.html")` to `return render_template("index.html", incoming_enabled=incoming.enabled)`.

(d) Replace the whole body of `scan_progress()` after the auth check (the `try: tasks = requests.get(...` block through `return jsonify({"error": str(e)}), 500`) with the block below, and add the new route after it:
```python
    # Only the real library scan (Key "RefreshLibrary"). Matching task *names*
    # also caught unrelated tasks like "Refresh Guide".
    try:
        task = downloads.fetch_library_task(JELLYFIN_URL, JELLYFIN_API_KEY)
    except downloads.JfError:
        app.logger.warning("scan progress: could not read Jellyfin scheduled tasks")
        return jsonify({"error": "Couldn't reach Jellyfin"}), 502
    if task and task.get("State") == "Running":
        pct = downloads._num(task.get("CurrentProgressPercentage"))
        return jsonify({"state": "running", "percent": round(pct, 1), "name": task.get("Name", "")})
    return jsonify({"state": "idle", "percent": 0})


@app.route("/api/downloads")
def api_downloads():
    if not authenticated():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        return jsonify(incoming.payload())
    except Exception:  # never let a raw exception (URLs, credentials) reach the page
        app.logger.exception("failed to build the downloads panel")
        return jsonify({"error": "Couldn't check downloads"}), 500
```

(e) In `scan()`, directly after `record_press(OUTCOME_STARTED, user=user)`, add:
```python
        # The panel's cached "ready to add" rows are about to become "adding now".
        incoming.invalidate()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests -q`
Expected: PASS — except `test_index_renders_incoming_panel_only_when_enabled`, which needs Task 5's markup (it asserts `id="incoming"`); it passes after Task 5.

---

### Task 5: Frontend — panel markup/CSS, the nudge, `incoming.js`, scan-console events

**Files:**
- Create: `app/static/incoming.js`
- Modify: `app/templates/index.html` (CSS, scan-console nudge slot, the Incoming section, script tag, three event dispatches)
- Modify: `app/tests/test_template.py` (append the Incoming section)

**Interfaces:**
- Consumes: template variable `incoming_enabled` (Task 4); `GET /api/downloads` JSON (Task 3/4 shape).
- Produces: DOM ids `incoming`, `incoming-title`, `incoming-count`, `incoming-loading`, `incoming-sources`, `incoming-downloading`, `incoming-more`, `incoming-downloading-empty`, `incoming-downloading-unknown`, `incoming-ready`, `incoming-ready-empty`, `incoming-ready-unknown`, `incoming-error`, `incoming-nudge`, `incoming-announce`; row classes `dl-row`, `ready-row`, `dl-name`, `src-tag`, `dl-meta`, `dl-state`, `src-problem`; DOM events `scan:cooldown` (`detail.until`, epoch ms, 0 = none) and `scan:started`. Task 6's browser tests select on exactly these.

- [ ] **Step 1: Write the failing tests.** Append to `app/tests/test_template.py`:

```python
# --- Incoming panel ---------------------------------------------------------

INCOMING_JS = Path(__file__).resolve().parents[1] / "static" / "incoming.js"


def test_incoming_js_never_uses_innerhtml():
    # Torrent names are attacker-chosen; rows must be built with textContent.
    assert not re.search(r"\.\s*innerHTML", INCOMING_JS.read_text(encoding="utf-8"))


def test_hidden_attribute_beats_class_display_rules():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important;?\s*\}", text)


def test_scan_console_announces_cooldown_and_start_events():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert text.count("new CustomEvent('scan:cooldown'") == 2      # start + end of cooldown
    assert "new CustomEvent('scan:started')" in text
    js = INCOMING_JS.read_text(encoding="utf-8")
    assert "addEventListener('scan:cooldown'" in js
    assert "addEventListener('scan:started'" in js
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_template.py app/tests/test_downloads.py -q`
Expected: FAIL — `incoming.js` does not exist, no `[hidden]` rule, no events, no `id="incoming"`.

- [ ] **Step 3: Implement.**

(a) Create `app/static/incoming.js`:

```javascript
/* Incoming panel: what qBittorrent is downloading, and what finished but is
   not in Jellyfin yet (so people know pressing Scan will add it).

   SECURITY: torrent names come from whoever made the torrent. Every value from
   /api/downloads is inserted with createElement + textContent ONLY. Never
   innerHTML.

   Coupling to the scan console is by DOM events only (dispatched by the inline
   script in index.html):
     scan:cooldown  detail.until = epoch ms the cooldown ends (0 = no cooldown)
     scan:started   a scan was just kicked off: refresh soon */
(function () {
  'use strict';

  const root = document.getElementById('incoming');
  if (!root) return;                        // feature off: the template rendered nothing

  const POLL_MS = 15000;
  const VISIBLE_ROWS = 8;

  const $ = (id) => document.getElementById(id);
  const sourcesEl    = $('incoming-sources');
  const countEl      = $('incoming-count');
  const dlList       = $('incoming-downloading');
  const dlEmpty      = $('incoming-downloading-empty');
  const dlUnknown    = $('incoming-downloading-unknown');
  const moreEl       = $('incoming-more');
  const readyList    = $('incoming-ready');
  const readyEmpty   = $('incoming-ready-empty');
  const readyUnknown = $('incoming-ready-unknown');
  const errorEl      = $('incoming-error');
  const loadingEl    = $('incoming-loading');
  const nudgeEl      = $('incoming-nudge');
  const announceEl   = $('incoming-announce');

  let pollTimer = null;
  let nudgeTimer = null;
  let inflight = false;
  let again = false;                        // a refresh was requested mid-flight
  let cooldownUntil = 0;
  let readyCount = 0;
  let addingCount = 0;
  let scanRunning = false;
  let lastAnnounced = null;

  const STATE_WORDS = {
    metadata: 'fetching metadata',
    checking: 'checking',
    stalled:  'stalled',
    queued:   'queued',
    paused:   'paused',
    error:    'error',
    stuck:    'stuck — recheck in qBittorrent',
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function fmtBytes(bytes) {
    let n = Math.max(0, num(bytes));
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    return (i === 0 ? String(Math.round(n)) : n.toFixed(n < 10 ? 1 : 0)) + ' ' + units[i];
  }

  function fmtEta(seconds) {
    const s = num(seconds);
    if (s <= 0) return '';
    if (s < 60) return '<1 min left';
    const min = Math.round(s / 60);
    if (min < 60) return min + ' min left';
    const h = Math.floor(min / 60);
    if (h < 48) return h + ' h ' + (min % 60) + ' min left';
    return Math.round(h / 24) + ' days left';
  }

  function fmtAgo(epochSeconds) {
    const sec = Math.round(Date.now() / 1000 - num(epochSeconds));
    if (sec < 60) return 'finished just now';
    const min = Math.round(sec / 60);
    if (min < 60) return 'finished ' + min + ' min ago';
    const h = Math.round(min / 60);
    if (h < 48) return 'finished ' + h + ' h ago';
    return 'finished ' + Math.round(h / 24) + ' days ago';
  }

  function fmtClock(ms) {
    const total = Math.max(0, Math.ceil(ms / 1000));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  function pctText(progress) {
    // An unfinished torrent never reads "100.0%" (0.9996 would round up).
    const pct = Math.min(99.9, Math.max(0, num(progress) * 100));
    return pct.toFixed(1) + '%';
  }

  function nameEl(className, name) {
    const span = el('span', className, name);
    span.title = String(name ?? '');        // property assignment, not markup
    return span;
  }

  function downloadRow(d) {
    const state = typeof d.state === 'string' ? d.state : 'downloading';
    let cls = 'dl-row';
    if (state === 'paused') cls += ' is-paused';
    if (state === 'stuck' || state === 'error') cls += ' is-bad';
    const row = el('div', cls);

    const head = el('div', 'dl-head');
    head.appendChild(nameEl('dl-name', d.name));
    head.appendChild(el('span', 'src-tag', d.source));
    row.appendChild(head);

    const track = el('div', 'dl-track');
    track.setAttribute('aria-hidden', 'true');   // the percent is in the text below
    const fill = el('div', 'dl-fill');
    fill.style.width = (Math.min(1, Math.max(0, num(d.progress))) * 100).toFixed(1) + '%';
    track.appendChild(fill);
    row.appendChild(track);

    const parts = [pctText(d.progress)];
    if (state === 'downloading' && num(d.dlspeed) > 0) parts.push(fmtBytes(d.dlspeed) + '/s');
    const eta = state === 'downloading' ? fmtEta(d.eta) : '';
    if (eta) parts.push(eta);
    const meta = el('div', 'dl-meta', parts.join(' · '));
    if (STATE_WORDS[state]) {
      meta.appendChild(el('span', 'dl-state', STATE_WORDS[state]));
    }
    row.appendChild(meta);
    return row;
  }

  function readyRow(r) {
    const row = el('div', 'ready-row' + (r.adding ? ' is-adding' : ''));
    const head = el('div', 'dl-head');
    head.appendChild(nameEl('dl-name', r.name));
    head.appendChild(el('span', 'src-tag', r.source));
    row.appendChild(head);
    row.appendChild(el('div', 'dl-meta', r.adding ? 'adding now…' : fmtAgo(r.completed_at)));
    return row;
  }

  function render(d) {
    errorEl.hidden = true;
    loadingEl.hidden = true;

    const sources = Array.isArray(d.sources) ? d.sources : [];
    const bad = sources.filter((s) => s && !s.ok);
    const anyOk = sources.some((s) => s && s.ok);
    clear(sourcesEl);
    for (const s of bad) {
      sourcesEl.appendChild(el('div', 'src-problem', String(s.label) + ': ' + String(s.error || 'unavailable')));
    }
    sourcesEl.hidden = bad.length === 0;

    // Downloading. With no reachable box, "nothing downloading" would be a lie.
    const rows = Array.isArray(d.downloading) ? d.downloading : [];
    const total = Math.max(rows.length, num(d.downloading_total));
    clear(dlList);
    for (const r of rows.slice(0, VISIBLE_ROWS)) {
      if (r && typeof r === 'object') dlList.appendChild(downloadRow(r));
    }
    const more = total - Math.min(rows.length, VISIBLE_ROWS);
    moreEl.textContent = more > 0 ? '+' + more + ' more' : '';
    moreEl.hidden = more <= 0;
    dlEmpty.hidden = total > 0 || !anyOk;
    dlUnknown.hidden = anyOk;

    // Ready to add. "unknown" is shown as unknown, never as "nothing waiting".
    const unknown = d.ready_status !== 'ok';
    const ready = unknown || !Array.isArray(d.ready) ? [] : d.ready.filter((r) => r && typeof r === 'object');
    clear(readyList);
    for (const r of ready) readyList.appendChild(readyRow(r));
    readyUnknown.textContent = unknown ? 'Can’t check Jellyfin right now.' : 'Can’t tell until qBittorrent answers.';
    readyUnknown.hidden = !(unknown || !anyOk);
    readyEmpty.hidden = unknown || ready.length > 0 || !anyOk;

    readyCount = ready.filter((r) => !r.adding).length;
    addingCount = ready.length - readyCount;
    scanRunning = !!(d.scan && d.scan.running);

    const counts = [];
    if (total) counts.push(total + ' downloading');
    if (ready.length) counts.push(ready.length + ' ready');
    countEl.textContent = counts.join(' · ');

    renderNudge();
  }

  function renderNudge() {
    let text = '';
    if (addingCount > 0 && scanRunning) {
      text = 'Adding ' + plural(addingCount, 'download') + ' now…';
    } else if (readyCount > 0) {
      text = plural(readyCount, 'finished download') + ' '
        + (readyCount === 1 ? "isn't" : "aren't") + ' in Jellyfin yet — scan to add '
        + (readyCount === 1 ? 'it' : 'them') + '.';
      const left = cooldownUntil - Date.now();
      if (left > 0) text += ' Scan available in ' + fmtClock(left) + '.';
    }
    nudgeEl.textContent = text;
    nudgeEl.hidden = !text;

    // Keep the countdown in the nudge ticking only while it is on screen.
    const ticking = readyCount > 0 && cooldownUntil > Date.now();
    if (ticking && !nudgeTimer) nudgeTimer = setInterval(renderNudge, 1000);
    if (!ticking && nudgeTimer) { clearInterval(nudgeTimer); nudgeTimer = null; }

    // Screen readers hear it once per change of count, not every second.
    if (readyCount !== lastAnnounced) {
      lastAnnounced = readyCount;
      announceEl.textContent = readyCount > 0
        ? plural(readyCount, 'finished download') + ' ready to add to Jellyfin.'
        : '';
    }
  }

  async function refresh() {
    if (inflight) { again = true; return; }
    inflight = true;
    try {
      const r = await fetch('/api/downloads', { headers: { Accept: 'application/json' }, cache: 'no-store' });
      if (r.status === 401) { window.location.assign('/login'); return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      if (!d || d.enabled === false) { root.hidden = true; nudgeEl.hidden = true; stop(); return; }
      render(d);
    } catch (_) {
      loadingEl.hidden = true;
      errorEl.hidden = false;               // keep the last good render, just flag it
    } finally {
      inflight = false;
      if (again) { again = false; refresh(); }
    }
  }

  function stop() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function start() {
    stop();
    if (!document.hidden) pollTimer = setInterval(refresh, POLL_MS);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { stop(); return; }
    refresh();
    start();
  });

  document.addEventListener('scan:cooldown', (e) => {
    cooldownUntil = num(e.detail && e.detail.until);
    renderNudge();
  });

  document.addEventListener('scan:started', () => {
    // Jellyfin needs a moment to flip the task to Running.
    setTimeout(refresh, 1500);
    setTimeout(refresh, 6000);
  });

  refresh();
  start();
})();
```

(b) In `app/templates/index.html` CSS: change the selector `#history-count {` to `#history-count, #incoming-count {`. Insert this block directly above `  /* ---------- Responsive ---------- */`:

```css
  /* ---------- Incoming (qBittorrent) ---------- */

  /* A class that sets display must never beat the hidden attribute. */
  [hidden] { display: none !important; }

  .incoming-group {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .incoming-list {
    display: flex;
    flex-direction: column;
    font-family: var(--font-mono);
  }

  .dl-row, .ready-row {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    padding: 0.7rem 0 0.7rem 1rem;
    border-top: 1px solid var(--hairline-soft);
  }
  .dl-row:first-child, .ready-row:first-child { border-top: none; }
  .dl-row::before, .ready-row::before {
    content: '';
    position: absolute;
    left: 0;
    top: 1.28rem;
    width: 0.55rem;
    height: 1px;
    background: rgba(140, 180, 235, 0.40);
  }
  /* Lantern tick: finished, waiting for a scan. */
  .ready-row::before { background: rgba(255, 192, 105, 0.75); }

  .dl-head {
    display: flex;
    align-items: baseline;
    gap: 0.6rem;
    min-width: 0;
  }

  .dl-name {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-weight: 500;
    font-size: 0.8rem;
    color: var(--foam);
    cursor: help;
  }

  .src-tag {
    flex: none;
    max-width: 12ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-body);
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--benthos);
    border: 1px solid var(--hairline);
    border-radius: 999px;
    padding: 0.14rem 0.5rem;
  }

  .dl-track {
    height: 2px;
    border-radius: 1px;
    background: rgba(140, 180, 235, 0.18);
    overflow: hidden;
  }
  .dl-fill {
    height: 100%;
    background: linear-gradient(90deg, rgba(76, 242, 199, 0.35), var(--lumen));
    transition: width 0.6s ease;
  }

  .dl-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 0.2rem 0.6rem;
    font-family: var(--font-mono);
    font-size: 0.7rem;
    color: var(--benthos);
  }
  .dl-state { color: var(--lantern); }

  .is-paused { opacity: 0.55; }
  .is-paused .dl-fill { background: var(--benthos); }
  .is-bad .dl-fill { background: var(--coral); }
  .is-bad .dl-state { color: var(--coral); }
  .ready-row.is-adding .dl-meta { color: var(--lumen); }

  .incoming-sources {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .incoming-empty, .incoming-more, .src-problem {
    font-size: 0.78rem;
    color: var(--benthos);
  }
  .incoming-more { padding-left: 1rem; }
  .incoming-empty { padding: 0.35rem 0; }
  .incoming-empty.warn, .src-problem { color: var(--coral); opacity: 0.9; }
  .src-problem { font-family: var(--font-mono); font-size: 0.74rem; }

  #incoming-nudge {
    font-size: 0.84rem;
    line-height: 1.45;
    color: var(--lantern);
    text-align: center;
    text-wrap: balance;
  }
```

and inside the existing `@media (prefers-reduced-motion: reduce) { ... }` block add `    .dl-fill { transition: none; }` after `#status-bar-fill { transition: none; }`.

(c) Directly after `<button id="scan-btn">Scan media library</button>` add:

```html
      {% if incoming_enabled %}
      <div id="incoming-nudge" hidden></div>
      <div id="incoming-announce" class="sr-only" role="status" aria-live="polite"></div>
      {% endif %}
```

(d) Directly above `  <!-- DIVE LOG -->` add:

```html
  {% if incoming_enabled %}
  <!-- INCOMING -->
  <section class="station" id="incoming" aria-labelledby="incoming-title">
    <div class="tick-label">Incoming</div>
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          <div class="eyebrow">qBittorrent</div>
          <h2 id="incoming-title">Incoming</h2>
        </div>
        <span id="incoming-count"></span>
      </div>
      <div id="incoming-loading" class="incoming-empty">Checking qBittorrent…</div>
      <div id="incoming-sources" class="incoming-sources" hidden></div>
      <div class="incoming-group">
        <div class="eyebrow">Downloading</div>
        <div id="incoming-downloading" class="incoming-list"></div>
        <div id="incoming-more" class="incoming-more" hidden></div>
        <div id="incoming-downloading-empty" class="incoming-empty" hidden>Nothing downloading right now.</div>
        <div id="incoming-downloading-unknown" class="incoming-empty warn" hidden>Can’t reach qBittorrent right now.</div>
      </div>
      <div class="incoming-group">
        <div class="eyebrow">Ready to add</div>
        <div id="incoming-ready" class="incoming-list"></div>
        <div id="incoming-ready-empty" class="incoming-empty" hidden>Nothing waiting for a scan.</div>
        <div id="incoming-ready-unknown" class="incoming-empty warn" hidden>Can’t check Jellyfin right now.</div>
      </div>
      <div id="incoming-error" class="incoming-empty warn" hidden>Couldn’t load downloads.</div>
    </div>
  </section>
  {% endif %}
```

(e) Directly after `<script src="{{ url_for('static', filename='jellyfield.js') }}"></script>` add (it must come BEFORE the inline script so its listeners exist when the inline script dispatches):

```html
{% if incoming_enabled %}
<script src="{{ url_for('static', filename='incoming.js') }}"></script>
{% endif %}
```

(f) In the inline script: in `startCooldown`, directly after `const until = Date.now() + remainingMs;` add
`    document.dispatchEvent(new CustomEvent('scan:cooldown', { detail: { until } }));`
— in the cooldown-finished branch, directly after `cooldownActive = false;` add
`        document.dispatchEvent(new CustomEvent('scan:cooldown', { detail: { until: 0 } }));`
— and in the click handler directly after `statusMsg.textContent = 'Scan started.';` add
`      document.dispatchEvent(new CustomEvent('scan:started'));`

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests -q`
Expected: PASS (all, including `test_index_renders_incoming_panel_only_when_enabled`).

---

### Task 6: Browser checks, screenshots, docs

**Files:**
- Create: `app/tests/test_incoming_e2e.py`
- Modify: `README.md` (Features bullet, a "Downloads panel (qBittorrent)" section, config-table row)
- Modify: `docker-compose.yml`, `.env.example` (commented `QBITTORRENT_INSTANCES` example)

**Interfaces:**
- Consumes: Task 5's DOM ids/classes/events; `app.incoming`, `app.authenticated`, `app.cooldown_remaining`, `app.history` (monkeypatched).
- Produces: `INCOMING_SHOTS=<dir>` screenshots for the owner's review.

- [ ] **Step 1: Write the browser tests.** Create `app/tests/test_incoming_e2e.py`:

```python
"""Browser checks for the Incoming panel (real Chrome via playwright).

Skipped when playwright or a browser isn't available (e.g. CI, which only
installs requirements + pytest). The backend is covered by test_downloads.py;
this file checks what actually renders.

Set INCOMING_SHOTS=/some/dir to also save screenshots of each state at phone
and desktop width for a human to look at.
"""

import os
import threading

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect

from werkzeug.serving import make_server

import app as app_module
from conftest import FakeResponse
from history import ScanHistory

NOW_S = 1_790_000_000


def body(**overrides):
    base = {
        "enabled": True,
        "checked_at": NOW_S,
        "sources": [{"label": "Box 1", "ok": True}, {"label": "Box 2", "ok": True}],
        "downloading": [
            {"source": "Box 1", "name": "The.Long.Light.2026.2160p.WEB-DL", "progress": 0.634,
             "state": "downloading", "dlspeed": 8_400_000, "eta": 1260, "size": 18_000_000_000},
            {"source": "Box 2", "name": "Perihelion.S01E04.1080p", "progress": 0.12,
             "state": "stalled", "dlspeed": 0, "eta": None, "size": 2_100_000_000},
            {"source": "Box 1", "name": "Stuck.Torrent.2025", "progress": 0.9996,
             "state": "stuck", "dlspeed": 0, "eta": None, "size": 900_000_000},
            {"source": "Box 2", "name": "Old.Paused.Thing", "progress": 0.4,
             "state": "paused", "dlspeed": 0, "eta": None, "size": 700_000_000},
        ],
        "downloading_total": 4,
        "ready": [
            {"source": "Box 1", "name": "Antinode.2026.1080p.BluRay", "completed_at": NOW_S - 720,
             "size": 9_000_000_000, "adding": False},
            {"source": "Box 2", "name": "Fractalarium.S02.COMPLETE.720p", "completed_at": NOW_S - 7200,
             "size": 14_000_000_000, "adding": False},
        ],
        "ready_status": "ok",
        "scan": {"running": False, "percent": 0},
    }
    base.update(overrides)
    return base


class Stub:
    def __init__(self):
        self.body = body()

    @property
    def enabled(self):
        return self.body.get("enabled", False)

    def payload(self):
        return self.body

    def invalidate(self):
        pass


@pytest.fixture
def server(tmp_path, monkeypatch):
    stub = Stub()
    state = {"auth": True, "cooldown": 0.0}
    monkeypatch.setattr(app_module, "incoming", stub)
    monkeypatch.setattr(app_module, "authenticated", lambda: state["auth"])
    monkeypatch.setattr(app_module, "cooldown_remaining", lambda: state["cooldown"])
    monkeypatch.setattr(app_module, "history", ScanHistory(str(tmp_path / "h.json")))
    monkeypatch.setattr(app_module, "JELLYFIN_URL", "http://jellyfin.test")
    monkeypatch.setattr(app_module, "JELLYFIN_API_KEY", "k")
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse(payload=[]))
    srv = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}", stub, state
    srv.shutdown()


@pytest.fixture(scope="module")
def browser():
    try:
        pw = playwright_sync.sync_playwright().start()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"playwright unavailable: {exc}")
    args = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
    # playwright's bundled build first, then a system Chrome (the bundled build
    # is often a different revision than the pip package expects).
    attempts = [{}] + [{"executable_path": p} for p in
                       (os.environ.get("CHROME_PATH"), "/usr/bin/google-chrome", "/usr/bin/chromium")
                       if p and os.path.exists(p)]
    b, errors = None, []
    for extra in attempts:
        try:
            b = pw.chromium.launch(args=args, **extra)
            break
        except Exception as exc:  # pragma: no cover - environment dependent
            errors.append(str(exc).splitlines()[0])
    if b is None:  # pragma: no cover - environment dependent
        pw.stop()
        pytest.skip("no usable chromium: " + " | ".join(errors))
    yield b
    b.close()
    pw.stop()


def open_page(browser, url, width=1440, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    # Freeze the page clock's "now" near the payload's timestamps.
    page.add_init_script(f"Date.now = () => {NOW_S * 1000};")
    page.goto(url)
    page.wait_for_selector("#incoming-loading", state="hidden")
    return page


def shot(page, name):
    target = os.environ.get("INCOMING_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.screenshot(path=os.path.join(target, name + ".png"), full_page=True)


def visible_text(page, selector):
    return [t.strip() for t in page.locator(selector).all_inner_texts()]


def test_rows_render_and_names_are_text_not_markup(server, browser):
    url, stub, _ = server
    evil = '<img src=x onerror="window.__pwned=1">'
    stub.body = body(downloading=[
        {"source": "Box 1", "name": evil, "progress": 0.5, "state": "downloading",
         "dlspeed": 1000, "eta": 60, "size": 1},
    ] + [{"source": "Box 2", "name": f"Filler {i}", "progress": 0.1, "state": "queued",
          "dlspeed": 0, "eta": None, "size": 1} for i in range(11)], downloading_total=12)
    page = open_page(browser, url)
    names = visible_text(page, "#incoming-downloading .dl-name")
    assert names[0] == evil
    assert len(names) == 8
    assert page.locator("#incoming-more").inner_text() == "+4 more"
    assert page.evaluate("window.__pwned") is None
    assert page.locator("#incoming-downloading img").count() == 0


def test_states_meta_and_nudge(server, browser):
    url, _, state = server
    state["cooldown"] = 1200.0
    for width, height in ((1440, 900), (390, 844)):
        page = open_page(browser, url, width, height)
        metas = visible_text(page, "#incoming-downloading .dl-meta")
        assert metas[0].startswith("63.4% · 8.4 MB/s · 21 min left")
        assert "stalled" in metas[1]
        assert metas[2].startswith("99.9%") and "stuck — recheck in qBittorrent" in metas[2]
        assert "paused" in metas[3]
        assert visible_text(page, "#incoming-ready .dl-meta") == ["finished 12 min ago", "finished 2 h ago"]
        # The cooldown arrives by event after /api/scan/state answers: let expect() wait for it.
        expect(page.locator("#incoming-nudge")).to_have_text(
            "2 finished downloads aren't in Jellyfin yet — scan to add them. Scan available in 20:00.")
        expect(page.locator("#incoming-announce")).to_have_text("2 finished downloads ready to add to Jellyfin.")
        # No horizontal page scroll at phone width.
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        shot(page, f"incoming-cooldown-{width}")
        page.close()


def test_nudge_without_cooldown_and_while_adding(server, browser):
    url, stub, _ = server
    page = open_page(browser, url)
    assert page.locator("#incoming-nudge").inner_text() == \
        "2 finished downloads aren't in Jellyfin yet — scan to add them."
    shot(page, "incoming-ready-1440")
    page.close()

    stub.body = body(scan={"running": True, "percent": 40.0},
                     ready=[dict(r, adding=True) for r in body()["ready"]])
    page = open_page(browser, url)
    assert page.locator("#incoming-nudge").inner_text() == "Adding 2 downloads now…"
    assert visible_text(page, "#incoming-ready .dl-meta") == ["adding now…", "adding now…"]
    page.close()


def test_unknown_and_down_sources_are_never_shown_as_zero(server, browser):
    url, stub, _ = server
    stub.body = body(ready=[], ready_status="unknown",
                     sources=[{"label": "Box 1", "ok": True},
                              {"label": "Box 2", "ok": False, "error": "unreachable"}])
    page = open_page(browser, url)
    assert page.locator("#incoming-ready-unknown").is_visible()
    assert not page.locator("#incoming-ready-empty").is_visible()
    assert not page.locator("#incoming-downloading-unknown").is_visible()   # Box 1 still answers
    assert visible_text(page, ".src-problem") == ["Box 2: unreachable"]
    assert not page.locator("#incoming-nudge").is_visible()
    shot(page, "incoming-degraded-1440")
    page.close()

    stub.body = body(downloading=[], downloading_total=0, ready=[], ready_status="unknown",
                     sources=[{"label": "Box 1", "ok": False, "error": "login failed"},
                              {"label": "Box 2", "ok": False, "error": "unreachable"}])
    page = open_page(browser, url, 390, 844)
    assert not page.locator("#incoming-downloading-empty").is_visible()
    assert page.locator("#incoming-downloading-unknown").inner_text() == "Can’t reach qBittorrent right now."
    assert page.locator("#incoming-ready-unknown").inner_text() == "Can’t check Jellyfin right now."
    assert visible_text(page, ".src-problem") == ["Box 1: login failed", "Box 2: unreachable"]
    shot(page, "incoming-all-down-390")
    page.close()


def test_quiet_state(server, browser):
    url, stub, _ = server
    stub.body = body(downloading=[], downloading_total=0, ready=[])
    page = open_page(browser, url, 390, 844)
    assert page.locator("#incoming-downloading-empty").is_visible()
    assert page.locator("#incoming-ready-empty").is_visible()
    assert not page.locator("#incoming-nudge").is_visible()
    assert page.locator("#incoming-count").inner_text() == ""
    shot(page, "incoming-quiet-390")
    page.close()


def test_disabled_renders_nothing(server, browser):
    url, stub, _ = server
    stub.body = {"enabled": False}
    page = browser.new_page()
    page.goto(url)
    assert page.locator("#incoming").count() == 0
    assert page.locator("#incoming-nudge").count() == 0
    page.close()


def test_hostile_widths_labels_and_future_times(server, browser):
    url, stub, _ = server
    long_name = "A" * 240                       # one unbreakable token
    label = "<b>Box</b>"
    stub.body = body(
        sources=[{"label": label, "ok": True}],
        downloading=[{"source": label, "name": long_name, "progress": 0.5, "state": "downloading",
                      "dlspeed": 1, "eta": 5, "size": 1}],
        downloading_total=1,
        ready=[{"source": label, "name": long_name, "completed_at": NOW_S + 300,   # box clock ahead
                "size": 1, "adding": False}])
    page = open_page(browser, url, 390, 844)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    # text_content, not inner_text: the tag is CSS-uppercased on screen.
    assert page.locator("#incoming-downloading .src-tag").all_text_contents() == [label]
    assert page.locator("#incoming-downloading b").count() == 0
    assert visible_text(page, "#incoming-ready .dl-meta") == ["finished just now"]
    shot(page, "incoming-hostile-390")
    page.close()


def test_expired_session_goes_to_login(server, browser):
    url, _, state = server
    page = open_page(browser, url)
    state["auth"] = False
    page.evaluate("document.dispatchEvent(new CustomEvent('scan:started'))")
    page.wait_for_url("**/login", timeout=10_000)
    page.close()
```

- [ ] **Step 2: Run them** (they exercise Task 5's code, which already exists, so they should pass; a failure here is a real frontend bug — fix the frontend, not the test):

Run: `INCOMING_SHOTS=/tmp/incoming-shots python3 -m pytest app/tests/test_incoming_e2e.py -q`
Expected: 8 passed (~90 s; uses system Chrome if playwright's bundled build is missing). In CI (no browser) they SKIP.

- [ ] **Step 3: Look at every screenshot** in `/tmp/incoming-shots/` (Read each PNG). Check: no overflow at 390 px, names ellipsize, the nudge sits under the button in lantern, stuck rows coral, paused rows dimmed, the all-down state shows the two "can't reach/check" lines and no false "Nothing downloading".

- [ ] **Step 4: Docs.**

README — in `## Features`, after the scan-history bullet add:
```markdown
- **Downloads panel (optional)** — shows what qBittorrent is downloading on one or more boxes, and which finished downloads aren't in Jellyfin yet, with a hint that a scan will add them
```

README — add this section directly above `## Configuration`:
````markdown
## Downloads panel (qBittorrent)

Set `QBITTORRENT_INSTANCES` to show an **Incoming** panel between the scan console and the dive log:

```yaml
    environment:
      - QBITTORRENT_INSTANCES=Box 1=http://192.0.2.10:8080, Box 2=http://192.0.2.11:8080
```

- Comma-separated `label=url` pairs, one per qBittorrent WebUI. If a WebUI needs a login, put it in the URL: `http://user:pass@192.0.2.10:8080` (percent-encode `@`, `:`, `,` in the password). Without one, the app relies on qBittorrent's *Bypass authentication for clients in whitelisted IP subnets*.
- **Downloading** lists every unfinished torrent with progress, speed and ETA. A torrent whose `amount_left` went negative (the out-of-disk-space self-lock) is shown as **stuck — recheck in qBittorrent**.
- **Ready to add** lists torrents that finished *after the last completed library scan started* and that Jellyfin doesn't already have (matched by the torrent's folder or file name against recently added items). This assumes qBittorrent saves straight into the folders Jellyfin watches. If the last scan failed or was cancelled, anything finished in the last 48 hours counts.
- It is **read-only**: it never pauses, deletes or changes a torrent. Torrent names are visible to everyone who can sign in; hashes, paths, trackers and the qBittorrent addresses never leave the server.
- If a box or Jellyfin can't be reached the panel says so; it never shows "nothing downloading" when it simply couldn't look.
````

README — add this row to the configuration table after `TRUST_PROXY`:
```markdown
| `QBITTORRENT_INSTANCES` | unset | Optional. `label=url` pairs for the Downloads panel (see above). Unset hides the panel |
```

`docker-compose.yml` — after the `# - TRUST_PROXY=1` line add:
```yaml
      # Optional: show what qBittorrent is downloading and what's ready to scan in.
      # Comma-separated label=url pairs (see README "Downloads panel").
      # - QBITTORRENT_INSTANCES=Box 1=http://192.0.2.10:8080, Box 2=http://192.0.2.11:8080
```

`.env.example` — append:
```
# Optional: qBittorrent WebUIs for the Downloads panel, as label=url pairs
# QBITTORRENT_INSTANCES=Box 1=http://192.0.2.10:8080, Box 2=http://192.0.2.11:8080
```

- [ ] **Step 5: Full verification**

Run: `python3 -m pytest app/tests -q` → all pass (e2e included locally).
Run: `python3 -c "import ast,sys; ast.parse(open('app/app.py').read()); ast.parse(open('app/downloads.py').read())"` and `node --check app/static/incoming.js` → no output.
Then stop: the owner reviews the screenshots before the single commit.

---

## Execution notes (2026-09-26)

What actually changed from the tasks above while this plan was being carried out:

- **The Jellyfin helpers moved into a shared module, `app/jellyfin.py`**, because the same-day audit needed them in
  `app.py` too. That covers `JfError`, `get_json`, `library_task`, `task_busy` and `parse_time`, plus `post`,
  `list_users`, `find_user`, `get_user` and the MediaBrowser `Authorization` header (replacing the legacy
  `X-Emby-Token`, which newer Jellyfin ignores). `downloads.py` imports them. The time-parser tests live in
  `app/tests/test_jellyfin.py`.
- **A task counts as busy when it is Running or Cancelling** (`jellyfin.task_busy`), for both the panel's "adding
  now" flag and the scan console.
- **The frontend was built together with the audit's frontend fixes.** That work included a reworked scan state
  machine, one shared fetch helper, sign-out and the dive-log paging. So Task 5's inline-script anchors were applied
  at the equivalent points in the reworked script, not at the literal lines quoted above.
- **Execution method:** parallel lanes, one per set of files (backend/security, frontend, jellyfish renderer, ops).
  Each lane was implemented, adversarially reviewed and fixed, then everything was verified together and landed in
  one commit.
- **The integration review changed a few behaviours after the tasks above.** The spec describes the result:
  - qBittorrent 5.2+ logins answer `204`, and bad credentials answer `401`. Both are handled.
  - A negative `amount_left` is never complete. The real ENOSPC shape (`progress > 1`) now shows as `stuck`; the
    fixtures above used an impossible `progress 0.99`.
  - The payload cache is a true single flight, aged from when a build finishes, on the monotonic clock, with a
    bounded 15 s wait.
  - `invalidate()` no longer takes the build lock and runs after `scan_lock` is released.
  - File listings stop at the build deadline.
  - "Adding now" follows the console's new `scan:activity` event.
  - Examples use placeholder labels (`Box 2`), never real hostnames.
