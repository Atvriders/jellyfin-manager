"""The Incoming panel: qBittorrent downloads + "finished but not in Jellyfin yet".

Everything runs against scripted fakes: FakeQb stands in for a qBittorrent
WebUI (requests.Session-shaped), FakeJellyfin for requests.get against
Jellyfin. No network.
"""

import json
import threading
import time

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

    def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
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


def build(qbs, jellyfin=None, clock=None, mono=None):
    """An Incoming over the given {label: FakeQb} with a fixed wall clock.

    `mono` is the monotonic clock (cache age, build deadline); the real one
    unless a test needs to move time."""
    fakes = list(qbs.values())
    instances = [dl.Instance(label, f"http://qb{i}.test:8080") for i, label in enumerate(qbs)]
    jellyfin = jellyfin if jellyfin is not None else FakeJellyfin()
    return dl.Incoming(
        instances, "http://jellyfin.test", "api-key",
        session_factory=lambda it=iter(fakes): next(it),
        jf_get=jellyfin.get, clock=clock or (lambda: NOW), mono=mono,
    )


class BlockingQb(FakeQb):
    """A qBittorrent whose torrent list hangs until the test releases it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.block = True
        self.entered = threading.Event()
        self.release = threading.Event()

    def get(self, url, params=None, headers=None, timeout=None):
        if self.block and url.endswith("/api/v2/torrents/info"):
            self.entered.set()
            assert self.release.wait(10), "test never released the build"
        return super().get(url, params, headers, timeout)

    @property
    def info_calls(self):
        return [c for c in self.calls if c[1].endswith("/api/v2/torrents/info")]


def in_thread(fn, results=None):
    def run():
        value = fn()
        if results is not None:
            results.append(value)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


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
    # The ENOSPC self-lock as qBittorrent really reports it: amount_left =
    # wanted - done < 0 and progress = done / wanted, unclamped, so > 1.
    ({"progress": 1.0000312, "amount_left": -4096, "state": "stalledDL"}, False),
    ({"progress": 1.000016384, "amount_left": -16384, "state": "forcedDL"}, False),
    # An upload-family state is qBittorrent's own "finished" verdict.
    ({"progress": 1.00001, "amount_left": -1, "state": "stalledUP"}, True),
])
def test_is_complete(row, complete):
    assert dl.is_complete(row) is complete


def test_a_real_enospc_stuck_torrent_is_listed_as_stuck():
    """progress > 1 with a negative amount_left used to land in the "complete"
    bucket, and with completion_on = -1 it wasn't ready either: it vanished."""
    stuck = torrent("ENOSPC.Stuck", state="stalledDL", progress=1.0000312, amount_left=-4096,
                    completion_on=-1)
    body = build({"Box": FakeQb(torrents=[stuck])}).payload()
    assert body["downloading_total"] == 1
    assert body["downloading"] == [{"source": "Box", "name": "ENOSPC.Stuck", "progress": 1.0,
                                    "state": "stuck", "dlspeed": 0, "eta": None, "size": 1000}]
    assert body["ready"] == []


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


# ------------------------------------------------------------------ coverage point


def test_coverage_point_trusts_only_completed_runs():
    task = FakeJellyfin().task
    assert dl.coverage_point(task, NOW) == pytest.approx(LAST_SCAN_START, abs=1)
    for status in ("Failed", "Cancelled", "Aborted"):
        task["LastExecutionResult"]["Status"] = status
        assert dl.coverage_point(task, NOW) == NOW - dl.FALLBACK_WINDOW
    assert dl.coverage_point(None, NOW) == NOW - dl.FALLBACK_WINDOW
    assert dl.coverage_point({"LastExecutionResult": None}, NOW) == NOW - dl.FALLBACK_WINDOW


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


class Qb52(FakeQb):
    """qBittorrent 5.2+ (WebAPI 2.14): a good login answers 204 No Content with
    an empty body (setStatus(Ok), no data), and bad credentials answer 401."""

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(("POST", url, data, headers))
        if data.get("password") == self.password:
            self.logged_in = True
            return Resp(status=204, text="")
        return Resp(status=401, text="")


def test_qbittorrent_52_login_answers_204_and_that_is_success():
    fake = Qb52(torrents=[torrent("A")], password="pw")
    assert [t["name"] for t in client_for(fake, "admin", "pw").torrents()] == ["A"]
    assert [c[0] for c in fake.calls] == ["POST", "GET"]


def test_qbittorrent_52_wrong_password_answers_401_and_that_is_login_failed():
    with pytest.raises(dl.QbError, match="^login failed$"):
        client_for(Qb52(password="pw"), "admin", "nope").torrents()


@pytest.mark.parametrize("status,text", [(200, "Fails."), (200, ""), (401, ""), (403, "banned"), (500, "Ok.")])
def test_only_204_or_200_ok_is_a_successful_login(status, text):
    class Answer(FakeQb):
        def post(self, url, data=None, headers=None, timeout=None):
            self.calls.append(("POST", url, data, headers))
            return Resp(status=status, text=text)
    with pytest.raises(dl.QbError, match="^login failed$"):
        client_for(Answer(password="pw"), "admin", "pw").torrents()


def test_401_on_a_data_call_is_an_expired_session_too():
    class Says401(FakeQb):
        def get(self, url, params=None, headers=None, timeout=None):
            if self.expire_next:
                self.calls.append(("GET", url, params, headers))
                self.expire_next = False
                self.logged_in = False
                return Resp(status=401)
            return super().get(url, params, headers, timeout)
    fake = Says401(torrents=[torrent("A")], password="pw")
    client = client_for(fake, "admin", "pw")
    client.torrents()
    fake.expire_next = True
    assert [t["name"] for t in client.torrents()] == ["A"]
    assert [c[0] for c in fake.calls] == ["POST", "GET", "GET", "POST", "GET"]


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


# ------------------------------------------------------------------ Incoming payload


def test_disabled_without_instances():
    assert dl.Incoming([]).payload() == {"enabled": False}
    assert dl.Incoming([]).enabled is False


def test_downloading_rows_sorted_and_capped():
    rows = [torrent(f"p{i}", state="pausedDL", progress=0.5, completion_on=-1) for i in range(30)]
    rows.append(torrent("live", state="downloading", progress=0.1, dlspeed=500, eta=60, completion_on=-1))
    rows.append(torrent("stuck", state="stalledDL", progress=1.0000312, amount_left=-4096, completion_on=-1))
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
    mono = [100.0]

    class SlowItems(FakeJellyfin):
        def get(self, url, params=None, headers=None, timeout=None, allow_redirects=True):
            if url.endswith("/Items"):
                mono[0] += dl.BUILD_DEADLINE + 1     # one page ate the whole budget
            return super().get(url, params, headers, timeout, allow_redirects)

    items = [{"Path": f"/tv/X/e{i}.mkv", "DateCreated": iso(NOW - 10)} for i in range(400)]
    body = build({"Box": FakeQb(torrents=[torrent("New")])}, SlowItems(items=items),
                 mono=lambda: mono[0]).payload()
    assert body["ready_status"] == "unknown" and body["ready"] == []


def test_file_listing_stops_at_the_build_deadline():
    """Each torrent without a root folder costs one /torrents/files call (up
    to a 5 s timeout each). Past the deadline the rest are shown without
    asking — erring toward showing, as a failed listing does — instead of
    stretching the build (and every viewer waiting on it) without bound."""
    mono = [100.0]

    class SlowFiles(FakeQb):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/torrents/files"):
                mono[0] += 5
            return super().get(url, params, headers, timeout)

    rows = [torrent(f"Pack{i}", content_path="/tv", save_path="/tv", completion_on=int(NOW - 600 - i))
            for i in range(10)]
    qb = SlowFiles(torrents=rows)
    body = build({"Box": qb}, mono=lambda: mono[0]).payload()
    assert len([c for c in qb.calls if c[1].endswith("/torrents/files")]) == 3   # t=0, 5, 10; 15 > 12
    assert len(body["ready"]) == 10
    assert body["ready_status"] == "ok"


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
    mono = [100.0]
    qb = FakeQb(torrents=[])
    incoming = build({"Box": qb}, mono=lambda: mono[0])
    incoming.payload()
    incoming.payload()
    assert len(qb.calls) == 1                        # second call served from cache
    mono[0] += dl.CACHE_TTL
    incoming.payload()
    assert len(qb.calls) == 2                        # TTL expired
    incoming.invalidate()
    incoming.payload()
    assert len(qb.calls) == 3                        # invalidated


def test_cache_age_counts_from_when_the_build_finished():
    """The cache was stamped with the time the build STARTED, so a build that
    took CACHE_TTL or longer was already stale when it landed."""
    mono = [100.0]

    class SlowQb(FakeQb):
        def get(self, url, params=None, headers=None, timeout=None):
            mono[0] += dl.CACHE_TTL + 1
            return super().get(url, params, headers, timeout)

    qb = SlowQb(torrents=[])
    incoming = build({"Box": qb}, mono=lambda: mono[0])
    first = incoming.payload()
    assert incoming.payload() is first
    assert len(qb.calls) == 1


def test_viewers_waiting_on_a_slow_build_all_get_its_result():
    """Viewers that arrive while a build runs share it, however long it took.
    Before: each queued viewer then ran its own full build, one after another
    (builds at 0, 10.4, 20.8 s; the third viewer waited 30.8 s)."""
    mono = [100.0]
    qb = BlockingQb(torrents=[])
    incoming = build({"Box": qb}, mono=lambda: mono[0])
    results = []
    threads = [in_thread(incoming.payload, results)]
    assert qb.entered.wait(5)
    threads += [in_thread(incoming.payload, results) for _ in range(3)]
    time.sleep(0.2)                               # let them queue behind the build
    mono[0] += dl.CACHE_TTL + 5                   # the build ran past the TTL
    qb.release.set()
    for t in threads:
        t.join(5)
    assert len(results) == 4
    assert all(r is results[0] for r in results)
    assert len(qb.info_calls) == 1


def test_waiting_for_someone_elses_build_is_bounded(monkeypatch):
    """A viewer never waits on another viewer's build for longer than
    BUILD_WAIT: it gets the last payload, even a stale one. (Nothing freed a
    waiting server thread before, so slow boxes piled up threads until the
    whole app — /healthz, the scan button, sign-in — stopped answering.)"""
    monkeypatch.setattr(dl, "BUILD_WAIT", 0.2)
    mono = [100.0]
    qb = BlockingQb(torrents=[torrent("A", state="downloading", progress=0.5, completion_on=-1)])
    qb.block = False
    incoming = build({"Box": qb}, mono=lambda: mono[0])
    old = incoming.payload()
    mono[0] += dl.CACHE_TTL
    qb.block = True
    builder = in_thread(incoming.payload)
    assert qb.entered.wait(5)
    started = time.monotonic()
    assert incoming.payload() is old
    assert time.monotonic() - started < 2
    qb.release.set()
    builder.join(5)


def test_with_nothing_cached_a_bounded_wait_says_busy(monkeypatch):
    monkeypatch.setattr(dl, "BUILD_WAIT", 0.2)
    qb = BlockingQb(torrents=[])
    incoming = build({"Box": qb})
    builder = in_thread(incoming.payload)
    assert qb.entered.wait(5)
    with pytest.raises(dl.Busy):
        incoming.payload()
    qb.release.set()
    builder.join(5)


def test_invalidate_never_waits_for_a_running_build():
    """invalidate() took the lock a build holds for its whole run, so a scan
    press (which invalidates) waited on qBittorrent and Jellyfin."""
    qb = BlockingQb(torrents=[])
    incoming = build({"Box": qb})
    builder = in_thread(incoming.payload)
    assert qb.entered.wait(5)
    done = threading.Event()
    in_thread(lambda: (incoming.invalidate(), done.set()))
    assert done.wait(1), "invalidate() blocked behind the build"
    qb.release.set()
    builder.join(5)
    # The build that straddled the invalidate predates it: never served from cache.
    qb.block = False
    incoming.payload()
    assert len(qb.info_calls) == 2


def test_an_invalidate_mid_build_sends_only_later_arrivals_to_a_new_build():
    """Viewers already waiting when a press invalidates still share the build
    they queued for (else each waited for it AND a second full build, past the
    page's 20 s timeout); a viewer arriving after the invalidate gets a new one."""
    qb = BlockingQb(torrents=[])
    incoming = build({"Box": qb})
    early, late = [], []
    threads = [in_thread(incoming.payload, early)]
    assert qb.entered.wait(5)
    threads.append(in_thread(incoming.payload, early))
    time.sleep(0.1)
    incoming.invalidate()
    threads.append(in_thread(incoming.payload, late))
    time.sleep(0.1)
    qb.release.set()
    for t in threads:
        t.join(5)
    assert len(early) == 2 and early[0] is early[1]
    assert len(late) == 1 and late[0] is not early[0]
    assert len(qb.info_calls) == 2


def test_a_backward_wall_clock_step_does_not_freeze_the_cache():
    """Cache age is measured on the monotonic clock. On the wall clock, a step
    back of X seconds served the same payload for X + 10 s."""
    wall, mono = [NOW], [100.0]
    qb = FakeQb(torrents=[torrent("A", state="downloading", progress=0.5, completion_on=-1)])
    incoming = build({"Box": qb}, clock=lambda: wall[0], mono=lambda: mono[0])
    assert incoming.payload()["downloading_total"] == 1
    wall[0] -= 3600
    mono[0] += dl.CACHE_TTL
    qb.torrents = []
    assert incoming.payload()["downloading_total"] == 0


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


def test_downloads_endpoint_busy_is_503(auth, monkeypatch):
    class Busy(StubIncoming):
        def payload(self):
            raise dl.Busy()
    monkeypatch.setattr(app_module, "incoming", Busy({"enabled": True}))
    r = auth.get("/api/downloads")
    assert r.status_code == 503
    assert r.get_json() == {"error": "Still checking downloads"}


def test_a_scan_press_never_waits_for_a_downloads_build(auth, monkeypatch, hist_path):
    """The press invalidated the panel's cache while holding scan_lock, and
    invalidate() waited for the running build: a started scan answered after
    the page's 20 s timeout (shown as a failure), and every other press queued
    behind it instead of getting its instant 429."""
    qb = BlockingQb(torrents=[])
    incoming = build({"Box": qb})
    monkeypatch.setattr(app_module, "incoming", incoming)
    monkeypatch.setattr(app_module.requests, "post", lambda *a, **kw: FakeResponse(status_code=204))
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse(
        payload=[{"Key": "RefreshLibrary", "State": "Idle"}]))
    viewer = in_thread(incoming.payload)
    assert qb.entered.wait(5)
    try:
        started = time.monotonic()
        first = auth.post("/api/scan")
        second = auth.post("/api/scan")
        elapsed = time.monotonic() - started
    finally:
        qb.release.set()
        viewer.join(5)
    assert (first.status_code, second.status_code) == (200, 429)
    assert elapsed < 2


def test_progress_seeing_the_scan_start_or_stop_drops_the_downloads_cache(auth, monkeypatch):
    """The panel's "adding now…" rows follow the scan console: when the
    progress endpoint sees the library scan start or stop, the cached panel
    payload (which says the opposite) is dropped, so the panel's next poll is
    fresh instead of up to CACHE_TTL behind."""
    stub = StubIncoming({"enabled": True})
    monkeypatch.setattr(app_module, "incoming", stub)
    task = {"Key": "RefreshLibrary", "State": "Idle"}
    fail = []

    def get(url, **kw):
        if fail:
            raise requests.ConnectionError("down")
        return FakeResponse(payload=[dict(task)])

    monkeypatch.setattr(app_module.requests, "get", get)

    def seen(state):
        task["State"] = state
        assert auth.get("/api/scan/progress").status_code == 200
        return stub.invalidated

    base = seen("Idle")
    assert seen("Idle") == base                  # no change, no invalidation
    assert seen("Running") == base + 1           # started
    assert seen("Running") == base + 1
    fail.append(True)                            # an unreadable answer is not "stopped"
    assert auth.get("/api/scan/progress").status_code == 502
    fail.clear()
    assert seen("Running") == base + 1
    assert seen("Idle") == base + 2              # ended


def test_index_renders_incoming_panel_only_when_enabled(auth, monkeypatch):
    monkeypatch.setattr(app_module, "incoming", dl.Incoming([]))
    assert 'id="incoming"' not in auth.get("/").get_data(as_text=True)
    monkeypatch.setattr(app_module, "incoming", StubIncoming({"enabled": True}))
    page = auth.get("/").get_data(as_text=True)
    assert 'id="incoming"' in page
    assert 'id="incoming-nudge"' in page
    assert "incoming.js" in page
