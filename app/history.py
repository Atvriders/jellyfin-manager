"""Persistent, crash-safe history of "Scan Media Library" button presses.

Every press is recorded with its outcome, so the page can show what happened and
the 1-hour cooldown can be *derived from disk* instead of a process-local global
(which a container restart would happily wipe).

Design notes:
  * Storage is a single JSON array. It is capped at 500 entries and presses are
    rate-limited to one per hour, so rewriting the whole file per press is cheap.
  * Writes are atomic (tmp file + os.replace). A container killed mid-write can
    never leave a truncated file behind.
  * Every read-modify-write is guarded by a lock: the Flask dev server is
    threaded, so two simultaneous presses are a real possibility.
  * A damaged history file must NEVER stop the app from booting or the scan
    button from working. It gets quarantined and we start fresh — with a
    WARNING, a copy that no later quarantine can overwrite, and at most
    MAX_QUARANTINED such copies kept.
  * Rejected presses ("cooldown", "busy") are coalesced: consecutive ones by
    the same user+ip within COALESCE_SECONDS bump a "count" on the previous
    row instead of appending, so holding the button down can't push the whole
    audit trail out of the 500-row cap in seconds.
"""

import errno
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid

from jellyfin import describe_status

OUTCOME_STARTED = "started"
OUTCOME_COOLDOWN = "cooldown"
OUTCOME_ERROR = "error"
# A library scan was already running, so no new one was requested. Like
# "cooldown" it is a rejected press: it never counts toward the cooldown.
OUTCOME_BUSY = "busy"

MAX_ENTRIES = 500

# Only rejected presses coalesce. A STARTED row IS the cooldown and an error
# carries its own reason; both must stay one row per press.
COALESCE_OUTCOMES = (OUTCOME_COOLDOWN, OUTCOME_BUSY)
COALESCE_SECONDS = 60
MAX_COUNT = 10**9

# A STARTED row dated further ahead than this can't be a real scan: the clock
# jumped backwards (NTP correction, RTC reset) after it was written. Honouring
# it would freeze the button for the size of the jump plus the cooldown.
FUTURE_SKEW_SECONDS = 60

# An attacker controls the User-Agent header and can send megabytes of it.
# Cap everything we persist so nobody can bloat the file.
MAX_TEXT_LEN = 256
MAX_IP_LEN = 64
MAX_USER_LEN = 64

# Quarantined copies are full of usernames and client IPs, and a file that
# keeps getting damaged would otherwise leave one behind per incident forever.
MAX_QUARANTINED = 5


def _describe_os_error(exc):
    """"Permission denied (EACCES)": the OS's words plus the errno name, which
    is what an operator searches for. Falls back to the exception's text."""
    text = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
    name = errno.errorcode.get(getattr(exc, "errno", None) or 0)
    return f"{text} ({name})" if name else text


def _clip(value, limit):
    """Coerce to str and truncate. Never raises."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


def _finite_number(value):
    """A real, finite JSON number (bool is an int in Python; it is not a time)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


# Rows written before the short fixed reasons (app/jellyfin.py) stored
# str(exception) from requests, which names the internal Jellyfin host and
# port — and every signed-in user sees the history. Recognised by the shapes
# requests produces; nothing in the fixed vocabulary contains any of them.
_RAW_HTTP_ERROR = re.compile(r"^(\d{3}) (?:Client|Server) Error\b")
_RAW_MARKERS = ("ConnectionPool(", "Max retries exceeded", "host=", "://", "Connection aborted", "Invalid URL")


def _scrub_error(text):
    """A stored error reason in the short, display-safe vocabulary."""
    match = _RAW_HTTP_ERROR.match(text)
    if match:
        return describe_status(int(match.group(1)))
    if any(marker in text for marker in _RAW_MARKERS):
        return "Jellyfin timed out" if "timed out" in text.lower() else "Jellyfin unreachable"
    return text


def _count(value):
    """A press count: a positive int, else 1. Never raises."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return min(value, MAX_COUNT)
    return 1


class ScanHistory:
    def __init__(self, path: str, max_entries: int = MAX_ENTRIES, clock=None, warn=None) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._clock = clock or time.time
        # Where warnings go (the app passes its logger): the clock jumping
        # back, and a damaged file being quarantined.
        self._warn = warn or logging.getLogger(__name__).warning
        self._warned_future_ts = None
        self._quarantine_stuck = False  # warned that a damaged file won't move

    # -- internals (callers must hold the lock) ----------------------------

    def _quarantine(self, reason: str) -> None:
        """Move an unreadable history file aside so we can start fresh.

        `reason` says what is wrong with the file, never what is in it: the
        rows hold usernames and client IPs, and log lines travel further than
        the file does.
        """
        # time_ns + random suffix: two quarantines inside one clock tick (or
        # on a clock that only ticks in seconds) still get different names.
        # The old fixed ".corrupt" name silently overwrote the previous copy.
        dest = f"{self.path}.corrupt-{time.time_ns()}-{os.urandom(3).hex()}"
        try:
            os.replace(self.path, dest)
        except OSError as exc:
            # Even this failing must not take the app down; the load just
            # starts from an empty list and the next write overwrites the file.
            # The file stays put, so EVERY read finds it again — and the page
            # polls. Warn once until the file reads cleanly again, not per call.
            if not self._quarantine_stuck:
                self._quarantine_stuck = True
                self._warn(
                    f"scan history {self.path!r} is damaged ({reason}) and could not be moved "
                    f"aside ({_describe_os_error(exc)}); ignoring it, so the history starts empty"
                )
            return
        self._quarantine_stuck = False
        self._warn(
            f"scan history {self.path!r} is damaged ({reason}); moved it aside to "
            f"{os.path.basename(dest)!r} and started a fresh history"
        )
        self._prune_quarantined(keep=dest)

    def _prune_quarantined(self, keep: str) -> None:
        """Delete all but the newest MAX_QUARANTINED copies. Best effort.

        Only names this class makes are touched (not the legacy ".corrupt"
        copy, not look-alikes). "Newest" is the time in the name, but the copy
        just made always stays: if the clock was pulled back since older
        copies were made, it would otherwise sort as the oldest and vanish
        right after the warning pointed at it.
        """
        directory = os.path.dirname(self.path) or "."
        pattern = re.compile(re.escape(os.path.basename(self.path)) + r"\.corrupt-(\d+)-([0-9a-f]{6})")
        try:
            names = os.listdir(directory)
        except OSError:
            return
        keep_name = os.path.basename(keep)
        others = []
        for name in names:
            match = pattern.fullmatch(name)
            if match and name != keep_name:
                others.append((int(match.group(1)), match.group(2), name))
        others.sort(reverse=True)
        for _ns, _suffix, name in others[MAX_QUARANTINED - 1 :]:
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass

    def _load(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return []
        except UnicodeDecodeError as exc:
            # A data-SHAPE problem (truncated / non-JSON / bad encoding): the
            # file really is damaged, so quarantine it and start fresh.
            # (UnicodeDecodeError is a ValueError: it must be caught first.)
            self._quarantine(f"not UTF-8 text (bad byte at offset {exc.start})")
            return []
        except json.JSONDecodeError as exc:
            # Line/column only: the exception's own message is fine, but its
            # .doc is the whole file.
            self._quarantine(f"not valid JSON (line {exc.lineno}, column {exc.colno})")
            return []
        except ValueError:
            self._quarantine("not valid JSON")
            return []
        # NOTE: a plain OSError (EMFILE "too many open files", EACCES, EIO, a
        # backup process momentarily holding the file, ...) is deliberately NOT
        # caught here. That is "could not read", not "damaged": treating it as
        # corruption would rename an intact file aside, return [], zero the
        # cooldown, and let the next write overwrite the whole history with a
        # 1-row file. Instead we let it propagate so callers can fail CLOSED.

        if not isinstance(raw, list):
            self._quarantine("its top level is not a list")
            return []
        self._quarantine_stuck = False  # readable again: a new failure is news

        # Tolerate junk rows inside an otherwise-valid list.
        entries = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            ts = row.get("ts")
            # json.load happily yields NaN/Infinity. A NaN STARTED ts turns the
            # cooldown arithmetic into NaN, and "NaN > 0" is False: a free scan.
            if not _finite_number(ts):
                continue
            outcome = row.get("outcome")
            if not isinstance(outcome, str):
                continue
            entry = {
                "id": _clip(row.get("id", ""), MAX_TEXT_LEN) or uuid.uuid4().hex,
                "ts": float(ts),
                "outcome": outcome,
                "ip": _clip(row.get("ip", ""), MAX_IP_LEN),
                "user_agent": _clip(row.get("user_agent", ""), MAX_TEXT_LEN),
                # Old rows may hold raw requests text (host:port); see _scrub_error.
                # The next save writes the scrubbed text back.
                "error": _scrub_error(_clip(row.get("error", ""), MAX_TEXT_LEN)),
                # Rows written before the Jellyfin-login feature have no
                # "user" key; default it so old history files load unchanged.
                "user": _clip(row.get("user", ""), MAX_USER_LEN),
                # Rows from before coalescing have no "count": each was 1 press.
                "count": _count(row.get("count")),
            }
            last_ts = row.get("last_ts")
            if _finite_number(last_ts) and last_ts >= entry["ts"]:
                entry["last_ts"] = float(last_ts)
            entries.append(entry)
        return entries

    def _coalesce_into(self, entries: list, entry: dict) -> "dict | None":
        """The previous row this rejected press folds into, or None."""
        if entry["outcome"] not in COALESCE_OUTCOMES or not entries:
            return None
        prev = entries[-1]  # "consecutive": only ever the newest row
        if (prev["outcome"], prev["user"], prev["ip"]) != (entry["outcome"], entry["user"], entry["ip"]):
            return None
        # Sliding window from the row's LAST press, so a steady flood stays one
        # row. A negative gap means the clock jumped back: start a new row.
        gap = entry["ts"] - prev.get("last_ts", prev["ts"])
        if not 0 <= gap <= COALESCE_SECONDS:
            return None
        return prev

    def _trim(self, entries: list) -> list:
        """Enforce the retention cap WITHOUT ever evicting the cooldown's
        source of truth.

        The persisted cooldown is derived from the most recent OUTCOME_STARTED
        row (see last_started_at). Plain newest-N trimming lets a flood of
        rejected 'cooldown' presses push that lone 'started' row off the end,
        which zeroes last_started_at() and hands out a free scan inside the
        cooldown window. So when the newest-N window would drop the most recent
        'started' row, we retain it as well (dropping an older non-started row
        to stay within the cap).
        """
        if self.max_entries < 0:
            return list(entries)
        if self.max_entries == 0:
            return []
        if len(entries) <= self.max_entries:
            return list(entries)

        kept = entries[-self.max_entries :]
        if any(e.get("outcome") == OUTCOME_STARTED for e in kept):
            return kept
        # The most recent 'started' row fell outside the window. It is older
        # than everything in `kept` (it sits before the tail), so re-attach it
        # as the oldest row and drop one more older row to respect the cap.
        for e in reversed(entries):
            if e.get("outcome") == OUTCOME_STARTED:
                return [e] + kept[1:]
        return kept

    def _save(self, entries: list) -> None:
        """Atomically replace the history file. Oldest-first on disk."""
        entries = self._trim(entries)

        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)

        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # atomic on POSIX

    # -- public API --------------------------------------------------------

    def storage_problem(self) -> "str | None":
        """Why saving the history would fail right now, or None if it works.

        Meant for startup, so a broken /data is reported when the container
        starts rather than discovered after presses quietly stopped being kept
        (the cooldown is derived from this file and would not survive a
        restart). It does what _save() does — create the directory, create a
        file in it, write and fsync it, rename it — but only ever on probe
        files of its own: the history file itself is only opened, to check
        that it can be, and never read, written or renamed over. Never raises.
        """
        directory = os.path.dirname(self.path) or "."
        if os.path.ismount(self.path):
            # A single-file bind mount (-v ./scan_history.json:/data/...).
            # Every save renames a new file over the history file, and the
            # kernel refuses to rename onto a mount point (EBUSY). The probe
            # below renames among its own files, so it could never see this.
            return (
                "the history file is itself a mount point (a single-file bind mount), and "
                "saving replaces it by renaming a new file over it, which a mount point "
                "refuses; mount the whole directory instead (e.g. ./data:/data)"
            )
        if os.path.exists(directory) and not os.path.isdir(directory):
            return f"{directory!r} exists but is not a directory"
        if os.path.isdir(self.path):
            # Docker creates a missing bind-mount source as a DIRECTORY, so a
            # single-file mount tried once leaves one here after the switch to
            # ./data:/data. The directory next to it is fine, so the probe
            # below passes — yet every read fails, and a cooldown that can't be
            # read fails closed: the button would stay in cooldown for good.
            return (
                f"{self.path!r} is a directory, not a file (Docker makes one for a single-file "
                "mount whose file didn't exist); remove it and the app creates the file"
            )
        if os.path.exists(self.path) and not os.path.isfile(self.path):
            # A FIFO, socket or device node: no better. Reading a FIFO blocks
            # until something writes to it, which would hang every request.
            return f"{self.path!r} is not a regular file"

        step = "create the directory"
        tmp = renamed = None
        try:
            os.makedirs(directory, exist_ok=True)
            step = "create a file in it"
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".write-probe-")
            try:
                # Real bytes, synced: a full disk can still hand out an empty
                # file, and only fails when data has to land.
                step = "write to it"
                os.write(fd, b"probe\n")
                os.fsync(fd)
            finally:
                os.close(fd)
            step = "rename files in it"
            # A sibling name of the probe's own, never the history file: that
            # file may hold the only record of the cooldown.
            renamed = tmp + ".renamed"
            os.replace(tmp, renamed)
            tmp = None
            step = "delete files in it"
            os.unlink(renamed)
            renamed = None
        except OSError as exc:
            return f"can't {step}: {_describe_os_error(exc)}"
        except Exception as exc:  # never let the probe itself stop the app booting
            return f"can't {step}: {type(exc).__name__}: {exc}"
        finally:
            for leftover in (tmp, renamed):
                if leftover:
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass

        # A save is a read-modify-write: record() loads the file first. So a
        # file this user may not read (another owner and mode 600, NFS
        # root_squash) fails every press as surely as a read-only directory,
        # though the directory probe above passes. Opened, never read.
        # O_NONBLOCK: should a FIFO appear there after the check above, the
        # open still can't hang startup.
        try:
            os.close(os.open(self.path, os.O_RDONLY | os.O_NONBLOCK))
        except FileNotFoundError:
            return None  # a first run: the first save creates it
        except OSError as exc:
            return f"can't read the history file: {_describe_os_error(exc)}"
        except Exception as exc:  # never raises, as above
            return f"can't read the history file: {type(exc).__name__}: {exc}"
        return None

    def record(self, outcome: str, ip: str = "", user_agent: str = "", error: str = "", user: str = "") -> dict:
        entry = {
            "id": uuid.uuid4().hex,
            "ts": self._clock(),
            "outcome": outcome,
            "ip": _clip(ip, MAX_IP_LEN),
            "user_agent": _clip(user_agent, MAX_TEXT_LEN),
            "error": _clip(error, MAX_TEXT_LEN),
            "user": _clip(user, MAX_USER_LEN),
            "count": 1,
        }
        with self._lock:
            entries = self._load()
            prev = self._coalesce_into(entries, entry)
            if prev is not None:
                prev["count"] = min(prev["count"] + 1, MAX_COUNT)
                prev["last_ts"] = entry["ts"]
                entry = prev
            else:
                entries.append(entry)
            self._save(entries)
        return dict(entry)

    def entries(self, limit: int = 50, offset: int = 0) -> list:
        """Newest first."""
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        with self._lock:
            items = self._load()
        items.reverse()  # stored oldest-first, exposed newest-first
        return items[offset : offset + limit]

    def count(self) -> int:
        """How many rows are stored (coalesced presses are one row)."""
        with self._lock:
            return len(self._load())

    def last_started_at(self) -> float:
        """Epoch seconds of the most recent successful scan, 0.0 if never.

        Only OUTCOME_STARTED counts: a press rejected by the cooldown (or by a
        scan already running), or one that failed to reach Jellyfin, must not
        extend the cooldown.

        STARTED rows dated more than FUTURE_SKEW_SECONDS ahead of the clock are
        ignored (and warned about once): the clock jumped back after they were
        written. The newest remaining start wins wherever it sits in the file,
        since a backwards jump also breaks "appended later == happened later".
        """
        with self._lock:
            items = self._load()
            now = self._clock()
            newest = 0.0
            future = None
            for entry in items:
                if entry.get("outcome") != OUTCOME_STARTED:
                    continue
                ts = float(entry.get("ts", 0.0))
                if ts > now + FUTURE_SKEW_SECONDS:
                    future = ts if future is None else max(future, ts)
                    continue
                newest = max(newest, ts)
            if future is not None and future != self._warned_future_ts:
                self._warned_future_ts = future
                self._warn(
                    f"scan history has a 'started' row {future - now:.0f}s in the future "
                    "(the system clock jumped backwards?); ignoring it for the cooldown"
                )
        return newest
