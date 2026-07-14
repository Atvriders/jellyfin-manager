import json
import os
import threading
import time

import pytest

from history import (
    MAX_ENTRIES,
    OUTCOME_COOLDOWN,
    OUTCOME_ERROR,
    OUTCOME_STARTED,
    ScanHistory,
)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "data" / "scan_history.json")


# --- basics ---------------------------------------------------------------


def test_contract_constants():
    assert OUTCOME_STARTED == "started"
    assert OUTCOME_COOLDOWN == "cooldown"
    assert OUTCOME_ERROR == "error"
    assert MAX_ENTRIES == 500


def test_empty_history_reads_clean(path):
    h = ScanHistory(path)
    assert h.entries() == []
    assert h.last_started_at() == 0.0


def test_record_returns_entry_with_full_shape(path):
    h = ScanHistory(path)
    e = h.record(OUTCOME_STARTED, ip="10.0.0.5", user_agent="curl/8", error="")

    assert set(e) == {"id", "ts", "outcome", "ip", "user_agent", "error"}
    assert isinstance(e["id"], str) and e["id"]
    assert isinstance(e["ts"], float)
    assert e["outcome"] == OUTCOME_STARTED
    assert e["ip"] == "10.0.0.5"
    assert e["user_agent"] == "curl/8"
    assert e["error"] == ""


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
    for _ in range(20):  # far past the cap, all rejected presses
        h.record(OUTCOME_COOLDOWN)

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
    for _ in range(MAX_ENTRIES):
        h.record(OUTCOME_COOLDOWN)

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
    for _ in range(10):
        h.record(OUTCOME_COOLDOWN)

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
    e = h.record(OUTCOME_STARTED, ip="192.168.1.9", user_agent="Mozilla/5.0 (X11)")
    assert e["ip"] == "192.168.1.9"
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
    assert os.path.exists(path + ".corrupt")

    # ...and the button still works afterwards.
    h.record(OUTCOME_STARTED, ip="1.2.3.4")
    assert len(ScanHistory(path).entries()) == 1


def test_json_that_is_not_a_list_is_quarantined(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"entries": ["nope"]}, f)

    h = ScanHistory(path)
    assert h.entries() == []
    assert os.path.exists(path + ".corrupt")

    with open(path + ".corrupt") as f:
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
    assert not os.path.exists(path + ".corrupt")

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
