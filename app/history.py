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
    button from working. It gets quarantined and we start fresh.
"""

import json
import os
import threading
import time
import uuid

OUTCOME_STARTED = "started"
OUTCOME_COOLDOWN = "cooldown"
OUTCOME_ERROR = "error"

MAX_ENTRIES = 500

# An attacker controls the User-Agent header and can send megabytes of it.
# Cap everything we persist so nobody can bloat the file.
MAX_TEXT_LEN = 256
MAX_IP_LEN = 64
MAX_USER_LEN = 64


def _clip(value, limit):
    """Coerce to str and truncate. Never raises."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value[:limit]


class ScanHistory:
    def __init__(self, path: str, max_entries: int = MAX_ENTRIES) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()

    # -- internals (callers must hold the lock) ----------------------------

    def _quarantine(self) -> None:
        """Move an unreadable history file aside so we can start fresh."""
        try:
            os.replace(self.path, self.path + ".corrupt")
        except OSError:
            # Even this failing must not take the app down; the load below just
            # starts from an empty list and the next write overwrites the file.
            pass

    def _load(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return []
        except (ValueError, UnicodeDecodeError):
            # A data-SHAPE problem (truncated / non-JSON / bad encoding): the
            # file really is damaged, so quarantine it and start fresh.
            self._quarantine()
            return []
        # NOTE: a plain OSError (EMFILE "too many open files", EACCES, EIO, a
        # backup process momentarily holding the file, ...) is deliberately NOT
        # caught here. That is "could not read", not "damaged": treating it as
        # corruption would rename an intact file aside, return [], zero the
        # cooldown, and let the next write overwrite the whole history with a
        # 1-row file. Instead we let it propagate so callers can fail CLOSED.

        if not isinstance(raw, list):
            self._quarantine()
            return []

        # Tolerate junk rows inside an otherwise-valid list.
        entries = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            outcome = row.get("outcome")
            if not isinstance(outcome, str):
                continue
            entries.append(
                {
                    "id": _clip(row.get("id", ""), MAX_TEXT_LEN) or uuid.uuid4().hex,
                    "ts": float(ts),
                    "outcome": outcome,
                    "ip": _clip(row.get("ip", ""), MAX_IP_LEN),
                    "user_agent": _clip(row.get("user_agent", ""), MAX_TEXT_LEN),
                    "error": _clip(row.get("error", ""), MAX_TEXT_LEN),
                    # Rows written before the Jellyfin-login feature have no
                    # "user" key; default it so old history files load unchanged.
                    "user": _clip(row.get("user", ""), MAX_USER_LEN),
                }
            )
        return entries

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

    def record(self, outcome: str, ip: str = "", user_agent: str = "", error: str = "", user: str = "") -> dict:
        entry = {
            "id": uuid.uuid4().hex,
            "ts": time.time(),
            "outcome": outcome,
            "ip": _clip(ip, MAX_IP_LEN),
            "user_agent": _clip(user_agent, MAX_TEXT_LEN),
            "error": _clip(error, MAX_TEXT_LEN),
            "user": _clip(user, MAX_USER_LEN),
        }
        with self._lock:
            entries = self._load()
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

    def last_started_at(self) -> float:
        """Epoch seconds of the most recent successful scan, 0.0 if never.

        Only OUTCOME_STARTED counts: a press rejected by the cooldown, or one
        that failed to reach Jellyfin, must not extend the cooldown.
        """
        with self._lock:
            items = self._load()
        for entry in reversed(items):
            if entry.get("outcome") == OUTCOME_STARTED:
                return float(entry.get("ts", 0.0))
        return 0.0
