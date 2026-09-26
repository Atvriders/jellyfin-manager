import errno
import glob
import json
import os
import re
import threading
import time

import pytest

from history import (
    COALESCE_SECONDS,
    FUTURE_SKEW_SECONDS,
    MAX_ENTRIES,
    OUTCOME_BUSY,
    OUTCOME_COOLDOWN,
    OUTCOME_ERROR,
    OUTCOME_STARTED,
    ScanHistory,
)


class Clock:
    """Injectable clock for ScanHistory(clock=...)."""

    def __init__(self, now=1_790_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "data" / "scan_history.json")


QUARANTINE_NAME = re.compile(r"scan_history\.json\.corrupt-(\d+)-[0-9a-f]{6}")


def quarantined(path):
    """The quarantined copies of `path`, oldest first (by the time in the name)."""
    copies = [p for p in glob.glob(glob.escape(path) + ".corrupt-*")
              if QUARANTINE_NAME.fullmatch(os.path.basename(p))]
    return sorted(copies, key=lambda p: int(QUARANTINE_NAME.fullmatch(os.path.basename(p)).group(1)))


def _write_raw(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# --- basics ---------------------------------------------------------------


def test_contract_constants():
    assert OUTCOME_STARTED == "started"
    assert OUTCOME_COOLDOWN == "cooldown"
    assert OUTCOME_ERROR == "error"
    assert OUTCOME_BUSY == "busy"
    assert MAX_ENTRIES == 500
    assert COALESCE_SECONDS == 60
    assert FUTURE_SKEW_SECONDS == 60


def test_empty_history_reads_clean(path):
    h = ScanHistory(path)
    assert h.entries() == []
    assert h.last_started_at() == 0.0


def test_record_returns_entry_with_full_shape(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_STARTED, ip="10.0.0.5", user_agent="curl/8", error="", user="alice")

    assert set(e) == {"id", "ts", "outcome", "ip", "user_agent", "error", "user", "count"}
    assert e["count"] == 1
    assert isinstance(e["id"], str) and e["id"]
    assert isinstance(e["ts"], float)
    assert e["outcome"] == OUTCOME_STARTED
    assert e["ip"] == "10.0.0.5"
    assert e["user_agent"] == "curl/8"
    assert e["error"] == ""
    assert e["user"] == "alice"


def test_record_read_round_trip_persists_to_disk(path):
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED, ip="1.2.3.4", user_agent="Firefox")

    # A completely fresh object over the same path sees it (i.e. it hit disk).
    reread = ScanHistory(path).entries()
    assert len(reread) == 1
    assert reread[0]["ip"] == "1.2.3.4"
    assert reread[0]["user_agent"] == "Firefox"

    # ...and the on-disk representation is a plain JSON array.
    with open(path) as f:
        raw = json.load(f)
    assert isinstance(raw, list)
    assert len(raw) == 1


def test_record_defaults_are_empty_strings(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_COOLDOWN)
    assert e["ip"] == ""
    assert e["user_agent"] == ""
    assert e["error"] == ""
    assert e["user"] == ""


def test_user_round_trips_to_disk(path):
    ScanHistory(path).record(OUTCOME_STARTED, user="bob")
    assert ScanHistory(path).entries()[0]["user"] == "bob"


def test_user_is_clipped_to_64_chars(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_STARTED, user="U" * 5000)
    assert len(e["user"]) == 64
    assert len(ScanHistory(path).entries()[0]["user"]) == 64


def test_old_history_file_without_user_keys_still_loads(path):
    """Pre-feature history files have no "user" key. They must load unchanged
    (defaulting user to "") and survive a full read-append-write round trip."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old_rows = [
        {"id": "a", "ts": 1.0, "outcome": "started", "ip": "1.1.1.1", "user_agent": "UA/1", "error": ""},
        {"id": "b", "ts": 2.0, "outcome": "error", "ip": "2.2.2.2", "user_agent": "UA/2", "error": "boom"},
    ]
    with open(path, "w") as f:
        json.dump(old_rows, f)

    h = ScanHistory(path)
    got = h.entries()  # newest first
    assert [e["id"] for e in got] == ["b", "a"]
    assert all(e["user"] == "" for e in got)
    assert h.last_started_at() == 1.0
    # It was NOT quarantined as damaged.
    assert quarantined(path) == []

    # Round trip: appending a new (user-stamped) row keeps the old rows.
    h.record(OUTCOME_STARTED, user="carol")
    reread = ScanHistory(path).entries()
    assert [e["user"] for e in reread] == ["carol", "", ""]
    assert [e["id"] for e in reread][1:] == ["b", "a"]


@pytest.mark.parametrize("raw,shown", [
    ("HTTPConnectionPool(host='10.0.0.5', port=8096): Max retries exceeded with url: /Library/Refresh "
     "(Caused by NewConnectionError('<urllib3.connection.HTTPConnection object at 0x7f>: Failed to "
     "establish a new connection: [Errno 111] Connection refused'))", "Jellyfin unreachable"),
    ("HTTPConnectionPool(host='10.0.0.5', port=8096): Read timed out. (read timeout=10)", "Jellyfin timed out"),
    ("HTTPSConnectionPool(host='jf.lan', port=8920): Max retries exceeded with url: /Library/Refresh "
     "(Caused by ConnectTimeoutError(<urllib3.connection.HTTPSConnection object>, 'Connection to jf.lan "
     "timed out. (connect timeout=10)'))", "Jellyfin timed out"),
    ("500 Server Error: Internal Server Error for url: http://10.0.0.5:8096/Library/Refresh",
     "Jellyfin returned HTTP 500"),
    ("401 Client Error: Unauthorized for url: http://10.0.0.5:8096/Library/Refresh",
     "Jellyfin rejected the API key (HTTP 401)"),
    ("('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))",
     "Jellyfin unreachable"),
    ("Invalid URL 'jellyfin:8096/Library/Refresh': No scheme supplied. Perhaps you meant "
     "https://jellyfin:8096/Library/Refresh?", "Jellyfin unreachable"),
])
def test_old_rows_with_raw_requests_text_are_shown_in_the_short_vocabulary(path, raw, shown):
    """Before the fixed reasons, a failed press stored str(exception) from
    requests: the internal Jellyfin host and port, shown to every signed-in
    user for as long as the row lasts (months, at ~1 press an hour and a
    500-row cap). Such rows are read back in the short vocabulary, and the next
    save rewrites them."""
    old_rows = [
        {"id": "a", "ts": 1.0, "outcome": "error", "ip": "1.1.1.1", "user_agent": "UA", "error": raw, "user": "bob"},
    ]
    _write_raw(path, json.dumps(old_rows))
    h = ScanHistory(path)
    [row] = h.entries()
    assert row["error"] == shown
    assert quarantined(path) == []
    h.record(OUTCOME_COOLDOWN)
    with open(path, encoding="utf-8") as f:
        on_disk = f.read()
    assert "10.0.0.5" not in on_disk and "jf.lan" not in on_disk and "jellyfin:8096" not in on_disk


@pytest.mark.parametrize("reason", [
    "Jellyfin unreachable", "Jellyfin timed out", "Jellyfin returned HTTP 503",
    "Jellyfin redirected (HTTP 302) - check JELLYFIN_URL", "Jellyfin rejected the API key (HTTP 401)",
    "JELLYFIN_URL or JELLYFIN_API_KEY not configured",
    "Jellyfin was slow to answer; the scan was probably started", "boom", "",
])
def test_rows_in_the_short_vocabulary_are_left_alone(path, reason):
    _write_raw(path, json.dumps([{"id": "a", "ts": 1.0, "outcome": "error", "error": reason}]))
    assert ScanHistory(path).entries()[0]["error"] == reason


def test_ids_are_unique(path):
    h = ScanHistory(path)
    ids = {h.record(OUTCOME_STARTED)["id"] for _ in range(50)}
    assert len(ids) == 50


def test_parent_directory_created_lazily(tmp_path):
    deep = str(tmp_path / "no" / "such" / "dir" / "scan_history.json")
    assert not os.path.exists(os.path.dirname(deep))

    h = ScanHistory(deep)
    assert h.entries() == []  # must not explode on a missing dir

    h.record(OUTCOME_STARTED)
    assert os.path.exists(deep)


# --- ordering & pagination ------------------------------------------------


def test_entries_are_newest_first(path):
    h = ScanHistory(path)
    for i in range(5):
        h.record(OUTCOME_STARTED, ip=str(i))

    got = [e["ip"] for e in h.entries()]
    assert got == ["4", "3", "2", "1", "0"]


def test_entries_limit_and_offset(path):
    h = ScanHistory(path)
    for i in range(10):
        h.record(OUTCOME_STARTED, ip=str(i))

    assert [e["ip"] for e in h.entries(limit=3)] == ["9", "8", "7"]
    assert [e["ip"] for e in h.entries(limit=3, offset=3)] == ["6", "5", "4"]
    assert h.entries(limit=3, offset=100) == []
    assert len(h.entries(limit=0)) == 0


def test_entries_default_limit_is_50(path):
    h = ScanHistory(path)
    for i in range(60):
        h.record(OUTCOME_STARTED, ip=str(i))

    got = h.entries()
    assert len(got) == 50
    assert got[0]["ip"] == "59"  # newest first


# --- retention ------------------------------------------------------------


def test_trim_drops_the_oldest(path):
    h = ScanHistory(path, max_entries=5)
    for i in range(10):
        h.record(OUTCOME_STARTED, ip=str(i))

    got = [e["ip"] for e in h.entries(limit=100)]
    assert got == ["9", "8", "7", "6", "5"]  # 0-4 dropped

    with open(path) as f:
        assert len(json.load(f)) == 5  # trimmed on disk, not just in the view


def test_trim_at_default_500(path):
    h = ScanHistory(path)
    for i in range(505):
        h.record(OUTCOME_STARTED, ip=str(i))

    with open(path) as f:
        raw = json.load(f)
    assert len(raw) == MAX_ENTRIES

    newest_first = h.entries(limit=1000)
    assert len(newest_first) == MAX_ENTRIES
    assert newest_first[0]["ip"] == "504"
    assert newest_first[-1]["ip"] == "5"  # 0-4 fell off the back


# --- retention must never evict the cooldown's source of truth ------------
# FINDINGS 1 / 3 / 6 regression.


def test_started_row_survives_a_cooldown_flood_small(path):
    """One 'started' then a flood of rejected 'cooldown' presses beyond the cap.
    Plain newest-N trimming would drop the lone 'started' row and zero the
    cooldown; it must be retained instead."""
    h = ScanHistory(path, max_entries=5)
    started = h.record(OUTCOME_STARTED)
    for i in range(20):  # far past the cap, all rejected presses
        # Distinct IPs: identical consecutive presses would coalesce into one
        # row and never reach the cap at all.
        h.record(OUTCOME_COOLDOWN, ip=f"10.0.0.{i}")

    assert h.last_started_at() == started["ts"]
    # ...and it's actually on disk, not just in memory.
    assert ScanHistory(path, max_entries=5).last_started_at() == started["ts"]
    # cap is still respected.
    with open(path) as f:
        assert len(json.load(f)) == 5


def test_started_row_survives_a_full_500_cooldown_flood(path):
    """The exact PoC from the review: 1 started + MAX_ENTRIES cooldown presses
    inside the hour must NOT evict the 'started' row (which would let the very
    next press fire a fresh Jellyfin scan well inside the cooldown)."""
    h = ScanHistory(path)  # default MAX_ENTRIES == 500
    started = h.record(OUTCOME_STARTED)
    for i in range(MAX_ENTRIES):
        h.record(OUTCOME_COOLDOWN, ip=str(i))  # distinct: no coalescing

    assert h.last_started_at() == started["ts"]
    assert h.last_started_at() != 0.0
    with open(path) as f:
        assert len(json.load(f)) == MAX_ENTRIES  # cap still honoured


def test_trim_keeps_only_the_most_recent_started(path):
    """When several 'started' rows exist, retention keeps the newest one as the
    cooldown source; older starteds may be trimmed like any other row."""
    h = ScanHistory(path, max_entries=4)
    h.record(OUTCOME_STARTED, ip="old-start")
    newest = h.record(OUTCOME_STARTED, ip="new-start")
    for i in range(10):
        h.record(OUTCOME_COOLDOWN, ip=str(i))

    assert h.last_started_at() == newest["ts"]


# --- last_started_at (the cooldown source of truth) ------------------------


def test_last_started_at_zero_when_never_started(path):
    h = ScanHistory(path)
    h.record(OUTCOME_COOLDOWN)
    h.record(OUTCOME_ERROR, error="boom")
    assert h.last_started_at() == 0.0


def test_last_started_at_ignores_cooldown_and_error_entries(path):
    """A rejected or failed press must NOT extend the cooldown."""
    h = ScanHistory(path)
    started = h.record(OUTCOME_STARTED)
    time.sleep(0.01)
    h.record(OUTCOME_COOLDOWN)
    time.sleep(0.01)
    h.record(OUTCOME_ERROR, error="jellyfin down")

    assert h.last_started_at() == started["ts"]


def test_last_started_at_returns_most_recent_start(path):
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED)
    time.sleep(0.01)
    h.record(OUTCOME_COOLDOWN)
    newest = h.record(OUTCOME_STARTED)

    assert h.last_started_at() == newest["ts"]


def test_cooldown_survives_restart(path):
    """The whole point of persisting: a container restart must not clear it."""
    cooldown_seconds = 3600

    h1 = ScanHistory(path)
    h1.record(OUTCOME_STARTED, ip="1.1.1.1")

    # Simulate a restart: brand new object, brand new in-memory state, same disk.
    h2 = ScanHistory(path)
    remaining = h2.last_started_at() + cooldown_seconds - time.time()
    assert remaining > 0
    assert remaining == pytest.approx(cooldown_seconds, abs=5)


def test_stale_started_entry_does_not_hold_the_cooldown(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_STARTED)

    # Rewrite that entry's ts to two hours ago.
    with open(path) as f:
        raw = json.load(f)
    raw[0]["ts"] = e["ts"] - 7200
    with open(path, "w") as f:
        json.dump(raw, f)

    assert ScanHistory(path).last_started_at() + 3600 - time.time() < 0


# --- input capping --------------------------------------------------------


def test_oversized_user_agent_is_truncated(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_STARTED, user_agent="A" * 100000)

    assert len(e["user_agent"]) == 256
    assert len(ScanHistory(path).entries()[0]["user_agent"]) == 256


def test_oversized_error_and_ip_are_truncated(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_ERROR, ip="9" * 5000, error="E" * 5000)

    assert len(e["ip"]) == 64
    assert len(e["error"]) == 256


def test_short_values_are_not_padded_or_mangled(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_STARTED, ip="192.168.77.9", user_agent="Mozilla/5.0 (X11)")
    assert e["ip"] == "192.168.77.9"
    assert e["user_agent"] == "Mozilla/5.0 (X11)"


def test_non_string_values_are_coerced(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_ERROR, ip=None, user_agent=None, error=ValueError("nope"))
    assert isinstance(e["ip"], str)
    assert isinstance(e["user_agent"], str)
    assert "nope" in e["error"]


# --- corruption -----------------------------------------------------------


def test_unparseable_file_is_quarantined_and_history_still_works(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write('[{"id": "a", "ts": 1.0, "outc')  # truncated mid-write

    h = ScanHistory(path)
    assert h.entries() == []  # starts fresh, does not raise
    assert len(quarantined(path)) == 1

    # ...and the button still works afterwards.
    h.record(OUTCOME_STARTED, ip="1.2.3.4")
    assert len(ScanHistory(path).entries()) == 1


def test_json_that_is_not_a_list_is_quarantined(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"entries": ["nope"]}, f)

    h = ScanHistory(path)
    assert h.entries() == []
    [copy] = quarantined(path)

    with open(copy) as f:
        assert json.load(f) == {"entries": ["nope"]}  # original preserved for forensics


def test_corrupt_file_does_not_break_last_started_at(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("}}}garbage{{{")

    assert ScanHistory(path).last_started_at() == 0.0


def test_transient_oserror_on_read_is_not_treated_as_corruption(path, monkeypatch):
    """FINDING 5 regression: a transient OSError reading the file (EMFILE
    'too many open files', EACCES, EIO) is NOT damage. The intact file must not
    be quarantined, an empty list must not be returned (which would zero the
    cooldown and let the next write overwrite the history) -- the error must
    propagate so callers can fail closed."""
    import builtins

    h = ScanHistory(path)
    h.record(OUTCOME_STARTED, ip="1.1.1.1")

    real_open = builtins.open

    def flaky_open(file, mode="r", *args, **kwargs):
        # Only reads of the real history file blow up; the atomic .tmp write
        # (mode "w") and everything else is untouched.
        if file == path and "r" in mode and "w" not in mode and "a" not in mode:
            raise OSError(24, "Too many open files")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", flaky_open)

    with pytest.raises(OSError):
        h.last_started_at()
    with pytest.raises(OSError):
        h.entries()

    # The intact file must NOT have been renamed aside.
    assert quarantined(path) == []

    # Once the pressure clears, the original data is still there and correct.
    monkeypatch.setattr(builtins, "open", real_open)
    reread = ScanHistory(path).entries()
    assert len(reread) == 1
    assert reread[0]["ip"] == "1.1.1.1"


def test_junk_rows_inside_a_valid_list_are_dropped(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(
            ["a string", 42, None, {"id": "x", "ts": 5.0, "outcome": "started", "ip": "", "user_agent": "", "error": ""}],
            f,
        )

    h = ScanHistory(path)
    got = h.entries()
    assert len(got) == 1
    assert got[0]["id"] == "x"
    assert h.last_started_at() == 5.0


# --- durability & concurrency ---------------------------------------------


def test_write_is_atomic_no_tmp_file_left_behind(path):
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED)

    assert not os.path.exists(path + ".tmp")
    leftovers = [p for p in os.listdir(os.path.dirname(path)) if p.endswith(".tmp")]
    assert leftovers == []


def test_concurrent_records_lose_nothing(path):
    h = ScanHistory(path)
    threads = 20
    barrier = threading.Barrier(threads)
    errors = []

    def press(i):
        try:
            barrier.wait()  # maximise the overlap
            h.record(OUTCOME_STARTED, ip=f"10.0.0.{i}", user_agent=f"ua-{i}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    ts = [threading.Thread(target=press, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert errors == []

    on_disk = ScanHistory(path).entries(limit=1000)
    assert len(on_disk) == threads
    assert {e["ip"] for e in on_disk} == {f"10.0.0.{i}" for i in range(threads)}
    assert len({e["id"] for e in on_disk}) == threads


def test_concurrent_records_never_expose_a_truncated_file(path):
    """A reader racing a writer must always see valid JSON (atomic replace)."""
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED)
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    bad.append(data)
            except FileNotFoundError:
                pass
            except Exception as exc:
                bad.append(exc)

    r = threading.Thread(target=reader)
    r.start()
    try:
        for i in range(100):
            h.record(OUTCOME_STARTED, ip=str(i))
    finally:
        stop.set()
        r.join()

    assert bad == []


# --- press-flood coalescing -------------------------------------------------
# Consecutive "cooldown"/"busy" presses by the same user+ip within 60 s fold
# into the previous row, so one person holding the button can't push the audit
# history (500 rows) out in seconds.


def test_consecutive_cooldown_presses_coalesce(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    h.record(OUTCOME_STARTED, ip="10.0.0.1", user="alice")
    first = h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    clock.advance(10)
    second = h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")

    assert second["id"] == first["id"]
    assert second["count"] == 2
    assert second["last_ts"] == clock.now
    assert second["ts"] == first["ts"]  # the row keeps its FIRST press time

    rows = ScanHistory(path).entries()  # really on disk
    assert [r["outcome"] for r in rows] == [OUTCOME_COOLDOWN, OUTCOME_STARTED]
    assert rows[0]["count"] == 2
    assert rows[0]["last_ts"] == clock.now


def test_a_flood_is_one_row_with_a_sliding_window(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    for _ in range(200):  # a press every 30 s for 100 minutes
        h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
        clock.advance(30)
    rows = h.entries(limit=1000)
    assert len(rows) == 1
    assert rows[0]["count"] == 200


def test_busy_presses_coalesce_too(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    h.record(OUTCOME_BUSY, ip="10.0.0.1", user="alice")
    clock.advance(59)
    h.record(OUTCOME_BUSY, ip="10.0.0.1", user="alice")
    rows = h.entries()
    assert len(rows) == 1 and rows[0]["count"] == 2


@pytest.mark.parametrize("second", [
    {"outcome": OUTCOME_COOLDOWN, "ip": "10.0.0.2", "user": "alice"},   # other ip
    {"outcome": OUTCOME_COOLDOWN, "ip": "10.0.0.1", "user": "bob"},     # other user
    {"outcome": OUTCOME_BUSY, "ip": "10.0.0.1", "user": "alice"},       # other outcome
])
def test_different_press_starts_a_new_row(path, second):
    h = ScanHistory(path, clock=Clock())
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    h.record(second["outcome"], ip=second["ip"], user=second["user"])
    assert len(h.entries()) == 2


def test_only_consecutive_presses_coalesce(path):
    """bob pressing in between breaks alice's run: the history stays an honest
    timeline instead of rewriting an older row."""
    h = ScanHistory(path, clock=Clock())
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.2", user="bob")
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    assert [r["user"] for r in h.entries()] == ["alice", "bob", "alice"]
    assert all(r["count"] == 1 for r in h.entries())


def test_presses_more_than_60s_apart_do_not_coalesce(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    clock.advance(61)
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    assert [r["count"] for r in h.entries()] == [1, 1]


def test_clock_jumping_back_does_not_coalesce(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    clock.advance(-3600)
    h.record(OUTCOME_COOLDOWN, ip="10.0.0.1", user="alice")
    assert len(h.entries()) == 2


@pytest.mark.parametrize("outcome", [OUTCOME_STARTED, OUTCOME_ERROR])
def test_started_and_error_rows_never_coalesce(path, outcome):
    """A STARTED row IS the cooldown and an error carries its own reason:
    each must stay its own row."""
    h = ScanHistory(path, clock=Clock())
    h.record(outcome, ip="10.0.0.1", user="alice", error="x")
    h.record(outcome, ip="10.0.0.1", user="alice", error="x")
    assert len(h.entries()) == 2


# --- "count" / "last_ts" on disk ------------------------------------------------


def _write_rows(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(rows, f)


def _row(**extra):
    row = {"id": "r", "ts": 100.0, "outcome": "cooldown", "ip": "", "user_agent": "", "error": "", "user": ""}
    row.update(extra)
    return row


def test_rows_without_count_default_to_1(path):
    _write_rows(path, [_row()])
    [row] = ScanHistory(path).entries()
    assert row["count"] == 1
    assert "last_ts" not in row


@pytest.mark.parametrize("bad", ["7", 0, -3, True, 2.5, None, [], 10**30])
def test_junk_count_is_normalised_not_fatal(path, bad):
    _write_rows(path, [_row(count=bad)])
    [row] = ScanHistory(path).entries()
    assert isinstance(row["count"], int) and not isinstance(row["count"], bool)
    assert 1 <= row["count"] <= 10**9
    assert quarantined(path) == []


def test_valid_count_and_last_ts_round_trip(path):
    _write_rows(path, [_row(count=7, last_ts=160.5)])
    [row] = ScanHistory(path).entries()
    assert row["count"] == 7
    assert row["last_ts"] == 160.5


@pytest.mark.parametrize("bad", ["160", True, None, 50.0, float("nan"), float("inf")])
def test_junk_last_ts_is_dropped(path, bad):
    """last_ts must be a real time at or after the row's first press."""
    _write_rows(path, [_row(last_ts=bad)])
    [row] = ScanHistory(path).entries()
    assert "last_ts" not in row


@pytest.mark.parametrize("bad_ts", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_ts_rows_are_dropped(path, bad_ts):
    """json.load accepts NaN/Infinity. A NaN STARTED ts would make the cooldown
    arithmetic NaN (NaN > 0 is False) and hand out a free scan."""
    _write_rows(path, [_row(id="ok", outcome="started", ts=5.0), _row(id="bad", outcome="started", ts=bad_ts)])
    h = ScanHistory(path)
    assert [r["id"] for r in h.entries()] == ["ok"]
    assert h.last_started_at() == 5.0


def test_entries_always_expose_count(path):
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED)
    h.record(OUTCOME_ERROR, error="x")
    assert all(r["count"] == 1 for r in h.entries())


# --- count() --------------------------------------------------------------------


def test_count_is_the_number_of_stored_rows(path):
    h = ScanHistory(path, max_entries=5)
    assert h.count() == 0
    for i in range(3):
        h.record(OUTCOME_STARTED, ip=str(i))
    assert h.count() == 3
    for i in range(10):
        h.record(OUTCOME_STARTED, ip=str(i))
    assert h.count() == 5  # the cap, not the number of presses ever


def test_count_does_not_count_coalesced_presses_twice(path):
    h = ScanHistory(path, clock=Clock())
    for _ in range(4):
        h.record(OUTCOME_COOLDOWN, ip="10.0.0.1")
    assert h.count() == 1


def test_count_propagates_a_read_failure(path, monkeypatch):
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED)

    def boom():
        raise OSError(5, "I/O error")

    monkeypatch.setattr(h, "_load", boom)
    with pytest.raises(OSError):
        h.count()


# --- last_started_at: clock jumped backwards ---------------------------------------


def test_future_started_row_is_ignored(path):
    """A STARTED row more than 60 s in the future can't be a real scan: the
    clock jumped back after it was written. Honouring it would freeze the
    button for (jump + 1 h)."""
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    real = h.record(OUTCOME_STARTED, ip="real")
    clock.advance(7200)
    h.record(OUTCOME_STARTED, ip="from-the-future")
    clock.advance(-7200 + 10)  # NTP pulls the clock back two hours

    assert h.last_started_at() == real["ts"]


def test_started_row_within_the_skew_still_counts(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    e = h.record(OUTCOME_STARTED)
    clock.advance(-FUTURE_SKEW_SECONDS)  # exactly 60 s ahead: small drift, trust it
    assert h.last_started_at() == e["ts"]


def test_only_future_rows_means_no_baseline(path):
    clock = Clock()
    h = ScanHistory(path, clock=clock)
    h.record(OUTCOME_STARTED)
    clock.advance(-3600)
    assert h.last_started_at() == 0.0


def test_future_started_row_warns_once(path):
    warnings = []
    clock = Clock()
    h = ScanHistory(path, clock=clock, warn=warnings.append)
    h.record(OUTCOME_STARTED)
    clock.advance(-3600)
    for _ in range(5):
        h.last_started_at()
    assert len(warnings) == 1
    assert "future" in warnings[0].lower()


def test_last_started_at_is_the_newest_trustworthy_start(path):
    """Rows are appended in wall-clock order, which a backwards jump breaks:
    the cooldown baseline is the LATEST plausible start, whatever its position."""
    _write_rows(path, [_row(id="a", outcome="started", ts=500.0), _row(id="b", outcome="started", ts=400.0)])
    assert ScanHistory(path, clock=Clock(now=1000.0)).last_started_at() == 500.0


# --- quarantine: logged, collision-proof, bounded -------------------------------
# A damaged file is moved aside so the app can start fresh. The operator must
# hear about it (the history just "reset"), no earlier copy may be overwritten,
# and the copies — full of usernames and IPs — must not pile up forever.


def test_quarantine_warns_once_with_the_path_and_reason(path):
    warnings = []
    _write_raw(path, '[{"id": "a", "ts": 1.0, "outc')  # truncated mid-write
    h = ScanHistory(path, warn=warnings.append)
    assert h.entries() == []
    assert h.entries() == []  # the fresh start is not warned about again

    assert len(warnings) == 1
    [copy] = quarantined(path)
    assert path in warnings[0]
    assert os.path.basename(copy) in warnings[0]  # where to look
    assert "not valid JSON" in warnings[0]


@pytest.mark.parametrize("raw,reason", [
    ('{"entries": []}', "not a list"),
    (b"\xff\xfe not utf-8", "not UTF-8"),
])
def test_quarantine_reason_names_the_problem(path, raw, reason):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw if isinstance(raw, bytes) else raw.encode())
    warnings = []
    ScanHistory(path, warn=warnings.append).entries()
    assert len(warnings) == 1 and reason in warnings[0]


def test_quarantine_warning_never_contains_the_file_contents(path):
    """The rows hold usernames and client IPs; the log line must not."""
    _write_raw(path, '[{"user": "alice-private", "ip": "203.0.113.77", "user_agent": "Secret/1.0", "ts": ')
    warnings = []
    ScanHistory(path, warn=warnings.append).entries()
    assert len(warnings) == 1
    for secret in ("alice-private", "203.0.113.77", "Secret/1.0"):
        assert secret not in warnings[0]


def test_quarantine_logs_to_the_module_logger_by_default(path, caplog):
    import logging

    _write_raw(path, "}}} not json {{{")
    with caplog.at_level(logging.WARNING, logger="history"):
        ScanHistory(path).entries()
    assert [r.levelno for r in caplog.records if r.name == "history"] == [logging.WARNING]


def test_quarantine_name_is_collision_proof(path, monkeypatch):
    """Two quarantines inside one clock tick must not overwrite each other: the
    old fixed ".corrupt" name kept only the LAST damaged file."""
    monkeypatch.setattr(time, "time_ns", lambda: 1_790_000_000_000_000_000)
    h = ScanHistory(path, warn=lambda msg: None)
    for text in ("first damaged file {{", "second damaged file {{"):
        _write_raw(path, text)
        h.entries()

    copies = quarantined(path)
    assert len(copies) == 2
    for copy in copies:
        assert QUARANTINE_NAME.fullmatch(os.path.basename(copy))
    contents = set()
    for copy in copies:
        with open(copy) as f:
            contents.add(f.read())
    assert contents == {"first damaged file {{", "second damaged file {{"}


def test_only_the_newest_five_quarantined_copies_are_kept(path):
    h = ScanHistory(path, warn=lambda msg: None)
    for i in range(8):
        _write_raw(path, f"damaged #{i} {{{{")
        h.entries()

    copies = quarantined(path)
    assert len(copies) == 5
    kept = []
    for copy in copies:
        with open(copy) as f:
            kept.append(f.read())
    assert kept == [f"damaged #{i} {{{{" for i in range(3, 8)]  # the oldest three went


def test_the_copy_just_made_is_kept_even_if_the_clock_ran_back(path):
    """Five older copies stamped in the "future" (the clock was later pulled
    back) must not push out the copy the warning just pointed at."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    future = [f"{path}.corrupt-{9_000_000_000_000_000_000 + i}-abcdef" for i in range(5)]
    for name in future:
        _write_raw(name, "old")
    _write_raw(path, "fresh damage {{")

    ScanHistory(path, warn=lambda msg: None).entries()

    copies = quarantined(path)
    assert len(copies) == 5
    fresh = [c for c in copies if c not in future]
    assert len(fresh) == 1
    with open(fresh[0]) as f:
        assert f.read() == "fresh damage {{"


def test_pruning_leaves_other_files_alone(path):
    """Only this app's own .corrupt-<ns>-<hex> copies are ever deleted: not the
    legacy ".corrupt" copy, not another file's copies, not lookalikes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    directory = os.path.dirname(path)
    bystanders = [
        path + ".corrupt",                                        # legacy name
        path + ".corrupt-1-abcdef.bak",                           # lookalike
        os.path.join(directory, "other.json.corrupt-1-abcdef"),   # another file
        os.path.join(directory, "notes.txt"),
    ]
    for name in bystanders:
        _write_raw(name, "keep me")

    h = ScanHistory(path, warn=lambda msg: None)
    for i in range(7):
        _write_raw(path, f"damage {i} {{")
        h.entries()

    for name in bystanders:
        assert os.path.exists(name), name
    assert len(quarantined(path)) == 5


def test_a_failed_quarantine_warns_once_not_on_every_read(path, monkeypatch):
    """If the damaged file can't be moved aside (read-only /data), every read
    finds it again. The page polls: that must not be a warning per request."""
    _write_raw(path, "}}} not json {{{")
    real_replace = os.replace

    def refuse_quarantine(src, dst, *a, **kw):
        if ".corrupt-" in str(dst):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", refuse_quarantine)
    warnings = []
    h = ScanHistory(path, warn=warnings.append)
    for _ in range(5):
        assert h.entries() == []

    assert len(warnings) == 1
    assert path in warnings[0] and "Read-only file system" in warnings[0]
    with open(path) as f:
        assert f.read() == "}}} not json {{{"  # left where it was, untouched


def test_a_new_quarantine_failure_warns_again_after_a_good_read(path, monkeypatch):
    _write_raw(path, "}}} not json {{{")
    real_replace = os.replace
    state = {"refuse": True}

    def maybe_refuse(src, dst, *a, **kw):
        if state["refuse"] and ".corrupt-" in str(dst):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", maybe_refuse)
    warnings = []
    h = ScanHistory(path, warn=warnings.append)
    h.entries()
    _write_raw(path, "[]")          # repaired by hand
    h.entries()
    _write_raw(path, "{{ broken")   # ...and damaged again later
    h.entries()
    assert len(warnings) == 2


# --- storage_problem(): the startup write probe --------------------------------
# A /data that can't be written must be reported when the container starts, not
# discovered after presses silently stopped persisting (and the cooldown with
# them). The probe exercises what a save does, on probe files of its own only.


def _snapshot(directory):
    return sorted(os.listdir(directory)) if os.path.isdir(directory) else None


def test_storage_problem_is_none_for_a_writable_dir_and_leaves_nothing_behind(path):
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED, ip="1.1.1.1")
    before = _snapshot(os.path.dirname(path))
    stat = os.stat(path)
    with open(path, "rb") as f:
        content = f.read()

    assert h.storage_problem() is None

    assert _snapshot(os.path.dirname(path)) == before
    after = os.stat(path)
    assert (after.st_ino, after.st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
    with open(path, "rb") as f:
        assert f.read() == content


def test_storage_problem_never_renames_anything_onto_the_history_file(path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        seen.append((os.path.abspath(src), os.path.abspath(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", spy)
    assert ScanHistory(path).storage_problem() is None
    assert seen, "the probe must exercise a rename, like a real save"
    target = os.path.abspath(path)
    assert all(target not in pair for pair in seen)
    assert all(os.path.dirname(dst) == os.path.dirname(target) for _src, dst in seen)


def test_storage_problem_creates_a_missing_directory_like_a_save_would(tmp_path):
    deep = tmp_path / "not" / "yet" / "scan_history.json"
    assert ScanHistory(str(deep)).storage_problem() is None
    assert os.listdir(deep.parent) == []


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions")
def test_storage_problem_reports_a_read_only_directory(path):
    directory = os.path.dirname(path)
    os.makedirs(directory)
    os.chmod(directory, 0o555)
    try:
        problem = ScanHistory(path).storage_problem()
    finally:
        os.chmod(directory, 0o755)
    assert problem and "Permission denied" in problem
    assert os.listdir(directory) == []


@pytest.mark.parametrize("step,exc,words", [
    ("mkstemp", OSError(errno.EROFS, "Read-only file system"), "Read-only file system"),
    ("write", OSError(errno.ENOSPC, "No space left on device"), "No space left on device"),
    ("fsync", OSError(errno.EIO, "Input/output error"), "Input/output error"),
    ("replace", OSError(errno.EXDEV, "Invalid cross-device link"), "Invalid cross-device link"),
])
def test_storage_problem_reports_each_failing_step_and_cleans_up(path, monkeypatch, step, exc, words):
    import tempfile

    os.makedirs(os.path.dirname(path))

    def boom(*a, **kw):
        raise exc

    target = {"mkstemp": (tempfile, "mkstemp"), "write": (os, "write"),
              "fsync": (os, "fsync"), "replace": (os, "replace")}[step]
    monkeypatch.setattr(*target, boom)
    problem = ScanHistory(path).storage_problem()
    monkeypatch.undo()

    assert problem and words in problem
    assert os.listdir(os.path.dirname(path)) == []  # no probe file left behind


def test_storage_problem_when_the_directory_is_a_file(tmp_path):
    blocker = tmp_path / "data"
    blocker.write_text("I am a file, not a directory")
    problem = ScanHistory(str(blocker / "scan_history.json")).storage_problem()
    assert problem and "not a directory" in problem
    assert blocker.read_text() == "I am a file, not a directory"


def test_storage_problem_detects_a_single_file_bind_mount(path, monkeypatch):
    """docker run -v ./scan_history.json:/data/scan_history.json: every save
    renames a new file over a mount point, which the kernel refuses (EBUSY)."""
    os.makedirs(os.path.dirname(path))
    real_ismount = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: os.path.abspath(p) == os.path.abspath(path) or real_ismount(p))
    problem = ScanHistory(path).storage_problem()
    assert problem and "mount" in problem and "directory" in problem
    assert os.listdir(os.path.dirname(path)) == []


def test_storage_problem_when_the_history_file_is_a_directory(path):
    """What Docker leaves behind for a single-file mount whose host file didn't
    exist yet. The directory next to it is fine, so only a check of the path
    itself can see that no save (and no read) will ever work."""
    os.makedirs(path)
    problem = ScanHistory(path).storage_problem()
    assert problem and "is a directory" in problem and repr(path) in problem
    assert os.listdir(os.path.dirname(path)) == ["scan_history.json"]   # nothing added
    assert os.listdir(path) == []                                        # nothing touched


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_storage_problem_when_the_history_file_is_not_a_regular_file(path):
    """A FIFO there would hang every read (no writer ever comes)."""
    os.makedirs(os.path.dirname(path))
    os.mkfifo(path)
    problem = ScanHistory(path).storage_problem()
    assert problem and "not a regular file" in problem


def test_storage_problem_accepts_a_symlinked_history_file(path, tmp_path):
    """A symlink to a real file loads and saves like the file itself."""
    real = tmp_path / "elsewhere.json"
    real.write_text("[]")
    os.makedirs(os.path.dirname(path))
    os.symlink(real, path)
    assert ScanHistory(path).storage_problem() is None


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions")
def test_storage_problem_when_the_history_file_cannot_be_read(path):
    """Every save reads the file first (it is a read-modify-write), so a file
    this user can't read fails every press even in a writable directory."""
    h = ScanHistory(path)
    h.record(OUTCOME_STARTED, ip="1.1.1.1")
    os.chmod(path, 0)
    try:
        problem = h.storage_problem()
    finally:
        os.chmod(path, 0o644)
    assert problem and "read" in problem and "Permission denied" in problem
    assert [e["ip"] for e in h.entries()] == ["1.1.1.1"]   # left exactly as it was


def test_storage_problem_never_raises(path, monkeypatch):
    import tempfile

    def weird(*a, **kw):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(tempfile, "mkstemp", weird)
    problem = ScanHistory(path).storage_problem()
    assert problem and "something unexpected" in problem
