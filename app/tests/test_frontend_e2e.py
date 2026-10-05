"""Browser checks for the scan console, the dive log, the surface header and
the login page (real Chrome via playwright).

Skipped when playwright or a browser isn't available (e.g. CI, which only
installs requirements + pytest). The real Flask app runs on a free port in a
thread; Jellyfin is faked at the `requests` layer (GET /ScheduledTasks with the
RefreshLibrary task, POST /Library/Refresh, and the login endpoints from
conftest's JellyfinAuthMock).

Set FRONTEND_SHOTS=/some/dir to also save screenshots of each state at phone
and desktop width for a human to look at.
"""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
expect = playwright_sync.expect
# Generous default waits: these run next to other suites on a busy machine.
# Assertions about *how soon* something happens pass their own timeout.
expect.set_options(timeout=10_000)

from werkzeug.serving import make_server

import app as app_module
import conftest
from conftest import FakeResponse, JellyfinAuthMock

UA_CHROME_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
UA_CHROME_ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36")
IPV6 = "2001:db8:5a3c:8e10:b5d4:91f2:7c3e:a0d9"

DESKTOP = (1440, 900)
PHONE = (390, 844)


class Gate:
    """Holds back ONE answer: the next call that passes through while armed
    has its answer computed at once but delivered only on release(). That is
    a slow server that made up its mind before something else happened."""

    def __init__(self):
        self._lock = threading.Lock()
        self._armed = False
        self.held = threading.Event()
        self._go = threading.Event()

    def arm(self):
        with self._lock:
            self._armed = True

    def release(self):
        self._go.set()

    def pass_through(self, answer):
        with self._lock:
            hold, self._armed = self._armed, False
        if hold:
            self.held.set()
            self._go.wait(15)
        return answer


class FakeJellyfin(JellyfinAuthMock):
    """JellyfinAuthMock plus a library-scan task we can drive from the test."""

    def __init__(self):
        super().__init__()
        self.running = False
        self.percent = 0.0
        self.tasks_status = 200       # anything else: GET /ScheduledTasks fails
        self.refresh_status = 204     # anything else: POST /Library/Refresh fails
        self.start_on_refresh = True  # a successful refresh flips the task to Running
        # With start_on_refresh off: the run a refresh starts is over before
        # anyone looks, and this is how it ended (LastExecutionResult).
        self.finish_on_refresh = None
        self.refresh_exc = None       # raised by POST /Library/Refresh (after it "arrived")
        self.flap = False             # every other GET /ScheduledTasks fails
        self.flap_failures = 0
        self._flap_next_fails = False
        # How the last run ended (Jellyfin's LastExecutionResult); None: never ran.
        self.last = {"StartTimeUtc": "2026-09-26T09:00:00.0000000Z", "Status": "Completed"}
        self.tasks_delay = 0.0        # seconds GET /ScheduledTasks takes to answer
        self.tasks_gate = Gate()      # hold one /ScheduledTasks answer back
        self.inflight = 0
        self.max_inflight = 0
        self._lock = threading.Lock()

    def library_task(self):
        task = {
            "Name": "Scan Media Library",
            "Key": "RefreshLibrary",
            "State": "Running" if self.running else "Idle",
        }
        if self.last is not None:
            task["LastExecutionResult"] = self.last
        if self.running:
            task["CurrentProgressPercentage"] = self.percent
        return task

    def get(self, url, **kwargs):
        if url.endswith("/ScheduledTasks"):
            self.gets.append((url, kwargs))
            if self.tasks_delay:
                with self._lock:
                    self.inflight += 1
                    self.max_inflight = max(self.max_inflight, self.inflight)
                time.sleep(self.tasks_delay)
                with self._lock:
                    self.inflight -= 1
            if self.flap:
                with self._lock:
                    fail, self._flap_next_fails = self._flap_next_fails, not self._flap_next_fails
                    self.flap_failures += fail
                if fail:
                    return FakeResponse(status_code=500)
            if self.tasks_status != 200:
                return self.tasks_gate.pass_through(FakeResponse(status_code=self.tasks_status))
            # An unrelated task whose name contains "library" comes first: the
            # app must pick the task by Key, not by name.
            return self.tasks_gate.pass_through(FakeResponse(payload=[
                {"Name": "Extract library chapter images", "Key": "RefreshChapterImages", "State": "Idle"},
                self.library_task(),
            ]))
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        if url.endswith("/Library/Refresh"):
            self.calls.append((url, kwargs))
            if self.refresh_status >= 300:
                return FakeResponse(status_code=self.refresh_status)
            if self.start_on_refresh:
                self.running, self.percent = True, 3.0
            elif self.finish_on_refresh is not None:
                self.last = self.finish_on_refresh
            if self.refresh_exc is not None:
                raise self.refresh_exc
            return FakeResponse(status_code=self.refresh_status)
        return super().post(url, **kwargs)


class Server:
    def __init__(self, url, jf, state, hist_path):
        self.url = url
        self.jf = jf
        self.state = state
        self.hist_path = Path(hist_path)

    def seed(self, rows):
        """Write history rows (given newest first) the way the app stores them."""
        self.hist_path.write_text(json.dumps(list(reversed(rows))), encoding="utf-8")


def row(ago_s, outcome="cooldown", **fields):
    entry = {
        "id": uuid.uuid4().hex,
        "ts": time.time() - ago_s,
        "outcome": outcome,
        "ip": "198.51.100.23",
        "user_agent": UA_CHROME_WIN,
        "error": "",
        "user": "alice",
        "count": 1,
    }
    entry.update(fields)
    return entry


# Every distinct text the countdown shows while it is on screen.
TIMER_FRAMES = """
    window.__timer = [];
    new MutationObserver(() => {
      const wrap = document.getElementById('timer-wrap');
      const t = document.getElementById('timer');
      if (!wrap || !t || getComputedStyle(wrap).display === 'none') return;
      if (window.__timer[window.__timer.length - 1] !== t.textContent) window.__timer.push(t.textContent);
    }).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
"""


def _set_if_present(monkeypatch, name, value):
    if hasattr(app_module, name):
        monkeypatch.setattr(app_module, name, value)


@pytest.fixture
def server(tmp_path, monkeypatch):
    from history import ScanHistory

    jf = FakeJellyfin()
    state = {"auth": True}
    hist_path = tmp_path / "scan_history.json"
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    monkeypatch.setattr(app_module, "authenticated", lambda: state["auth"])
    monkeypatch.setattr(app_module, "history", ScanHistory(str(hist_path)))
    monkeypatch.setattr(app_module, "JELLYFIN_URL", "http://jellyfin.test")
    monkeypatch.setattr(app_module, "JELLYFIN_API_KEY", "api-key")
    monkeypatch.setattr(app_module.requests, "get", jf.get)
    monkeypatch.setattr(app_module.requests, "post", jf.post)
    # In-process state that must not leak between tests (names may not exist
    # in every version of app.py).
    _set_if_present(monkeypatch, "_last_started_fallback", 0.0)
    _set_if_present(monkeypatch, "_last_started_cache", 0.0)
    _set_if_present(monkeypatch, "_revoked_sids", {})
    _set_if_present(monkeypatch, "_history_writable", True)
    try:
        from limiter import LoginLimiter
        _set_if_present(monkeypatch, "login_limiter", LoginLimiter())
    except ImportError:  # pragma: no cover - older app.py
        pass
    try:
        import downloads
        monkeypatch.setattr(app_module, "incoming", downloads.Incoming([]))   # panel off here
    except Exception:  # pragma: no cover - older app.py
        pass
    monkeypatch.setattr(app_module.app, "secret_key", "e2e-secret")
    srv = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield Server(f"http://127.0.0.1:{srv.server_port}", jf, state, hist_path)
    jf.tasks_gate.release()          # never leave a request thread parked
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


def session_cookie(user="alice"):
    """A real signed session cookie for `user`, minted by the app itself."""
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        if hasattr(conftest, "sign_in"):
            conftest.sign_in(sess, user=user)
        else:  # pragma: no cover - older conftest
            sess["auth"] = True
            sess["user"] = user
    name = app_module.app.config.get("SESSION_COOKIE_NAME", "session")
    return name, client.get_cookie(name).value


class Page:
    """A page plus a log of the API requests it made."""

    def __init__(self, browser, server, size, signed_in=True, init_script=None, reduced_motion=False):
        # reduced_motion=True opens the page with prefers-reduced-motion, so the
        # background is the static 2D field, not the animated WebGL jellyfish
        ctx_kw = {"reduced_motion": "reduce"} if reduced_motion else {}
        self.context = browser.new_context(viewport={"width": size[0], "height": size[1]}, **ctx_kw)
        if signed_in:
            name, value = session_cookie()
            self.context.add_cookies([{"name": name, "value": value, "url": server.url}])
        self.page = self.context.new_page()
        self.requests = []
        self.page.on("request", lambda r: self.requests.append((r.method, r.url)))
        if init_script:
            self.page.add_init_script(init_script)
        self.server = server

    def open(self, path="/"):
        self.page.goto(self.server.url + path)
        return self.page

    def count(self, method, suffix):
        return sum(1 for m, u in self.requests if m == method and u.split("?")[0].endswith(suffix))

    def close(self):
        self.context.close()


@pytest.fixture
def pages(browser, server):
    opened = []

    def make(size=DESKTOP, **kw):
        p = Page(browser, server, size, **kw)
        opened.append(p)
        return p

    yield make
    for p in opened:
        p.close()


def shot(page, name):
    target = os.environ.get("FRONTEND_SHOTS")
    if target:
        os.makedirs(target, exist_ok=True)
        page.mouse.move(0, 0)               # resting state, not a hover
        page.wait_for_timeout(400)          # let 0.15 s colour transitions settle
        page.screenshot(path=os.path.join(target, name + ".png"), full_page=True)


def no_horizontal_scroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# ---------------------------------------------------------------- session


@pytest.mark.parametrize("trigger", ["press", "resync"])
def test_401_from_any_api_call_lands_on_login(server, pages, trigger):
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    server.state["auth"] = False
    p.context.clear_cookies()
    if trigger == "press":
        page.click("#scan-btn")
    else:
        # Coming back to the tab re-syncs at once; that answer is a 401.
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_url("**/login", timeout=10_000)


def test_sign_out_is_a_post_and_lands_on_login(server, pages):
    p = pages()
    page = p.open()
    expect(page.locator(".session")).to_contain_text("Signed in as alice")
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url("**/login", timeout=10_000)
    assert p.count("POST", "/logout") == 1
    assert p.count("GET", "/logout") == 0


# ---------------------------------------------------------------- scan console


def test_idle_page(server, pages):
    server.seed([row(7200, "started", user="alice"), row(9000, "cooldown", user="bob", ip=IPV6,
                                                       user_agent=UA_CHROME_ANDROID)])
    for size in (DESKTOP, PHONE):
        p = pages(size)
        page = p.open()
        expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
        expect(page.locator("#status-bar-wrap")).to_be_hidden()
        expect(page.locator("#timer-wrap")).to_be_hidden()
        expect(page.locator(".history-row")).to_have_count(2)
        assert no_horizontal_scroll(page)
        shot(page, f"idle-{size[0]}")


def test_scan_running_from_another_source_blocks_the_button(server, pages):
    server.jf.running, server.jf.percent = True, 42.0
    p = pages()
    page = p.open()
    btn = page.locator("#scan-btn")
    expect(page.locator("#status-bar-label")).to_have_text("Scan Media Library")
    expect(page.locator("#status-bar-pct")).to_have_text("42.0%")
    expect(page.locator("#status-msg")).to_have_text("A library scan is running.")
    expect(btn).to_have_attribute("aria-disabled", "true")
    assert page.locator("#status-bar-fill.indeterminate").count() == 0
    # The reason is part of the button's description.
    assert "status-msg" in btn.get_attribute("aria-describedby")

    btn.click(force=True)                          # guarded: nothing is sent
    page.wait_for_timeout(300)
    assert p.count("POST", "/api/scan") == 0
    assert server.jf.refresh_calls == []

    server.jf.percent = 77.5
    expect(page.locator("#status-bar-pct")).to_have_text("77.5%", timeout=6000)
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        shot(page, f"running-{size[0]}")

    server.jf.running = False
    expect(page.locator("#status-bar-label")).to_have_text("Scan complete", timeout=6000)
    expect(page.locator("#status-msg")).to_have_text("Ready to scan.")
    expect(btn).to_have_attribute("aria-disabled", "false")
    expect(page.locator("#status-bar-wrap")).to_be_hidden(timeout=6000)


def test_post_409_shows_already_running_and_tracks_progress(server, pages):
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    # A scan starts elsewhere after the page synced; the page doesn't know yet.
    server.jf.running, server.jf.percent = True, 12.0
    page.click("#scan-btn")
    expect(page.locator("#status-msg")).to_have_text("A library scan is already running.")
    assert page.locator("#status-msg.error").count() == 0
    expect(page.locator("#status-bar-pct")).to_have_text("12.0%", timeout=6000)
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "true")
    expect(page.locator("#timer-wrap")).to_be_hidden()            # a 409 starts no cooldown
    assert server.jf.refresh_calls == []                          # the running scan was not restarted
    expect(page.locator(".history-row .badge.busy")).to_have_count(1)


def test_press_starts_cooldown_with_no_stale_frame(server, pages):
    # Record every text the timer ever shows.
    p = pages(init_script=TIMER_FRAMES)
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    page.click("#scan-btn")
    expect(page.locator("#status-msg")).to_have_text("Scan started.")
    expect(page.locator("#timer-wrap")).to_be_visible()
    first = page.evaluate("window.__timer[0]")
    assert first in ("60:00", "59:59"), first            # never "00:00" from a previous cooldown
    expect(page.locator("#sr-announce")).to_have_text(
        "Scan cooldown active. Next scan available in about 60 minutes.")
    expect(page.locator("#scan-when")).to_have_text("Next scan available in about 60 minutes.")
    expect(page.locator("#status-bar-label")).to_have_text("Scan Media Library", timeout=6000)
    assert len(server.jf.refresh_calls) == 1
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        assert no_horizontal_scroll(page)
        shot(page, f"cooldown-{size[0]}")


def test_a_press_before_the_first_sync_waits_for_it(server, pages, monkeypatch):
    real = app_module.cooldown_remaining

    def slow_cooldown_remaining():
        time.sleep(1.5)
        return real()

    monkeypatch.setattr(app_module, "cooldown_remaining", slow_cooldown_remaining)
    p = pages()
    page = p.open()
    btn = page.locator("#scan-btn")
    assert btn.get_attribute("data-booting") is not None
    # Not dimmed while booting: no dim-then-bright flash on every load.
    assert page.evaluate("getComputedStyle(document.getElementById('scan-btn')).backgroundColor") \
        == "rgb(0, 164, 220)"
    btn.click(force=True)
    assert p.count("POST", "/api/scan") == 0            # held until the state is known
    expect(page.locator("#status-msg")).to_have_text("Scan started.")
    assert p.count("POST", "/api/scan") == 1
    assert btn.get_attribute("data-booting") is None


def test_keyboard_press_keeps_focus_on_the_button(server, pages):
    p = pages()
    page = p.open()
    btn = page.locator("#scan-btn")
    expect(btn).to_have_attribute("aria-disabled", "false")
    btn.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#status-msg")).to_have_text("Scan started.")
    expect(btn).to_have_attribute("aria-disabled", "true")
    assert page.evaluate("document.activeElement.id") == "scan-btn"
    page.keyboard.press("Enter")                         # the guard swallows it
    page.keyboard.press("Space")
    page.wait_for_timeout(300)
    assert p.count("POST", "/api/scan") == 1
    assert page.evaluate("document.activeElement.id") == "scan-btn"


def test_progress_502_shows_unavailable_and_backs_off(server, pages):
    # Reduced motion keeps the jellyfish still: this test counts the console's backed-off polls, which the software-rendered WebGL field can starve.
    p = pages(reduced_motion=True)
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    page.click("#scan-btn")
    expect(page.locator("#status-bar-label")).to_have_text("Scan Media Library", timeout=6000)
    server.jf.tasks_status = 500
    expect(page.locator("#status-bar-label")).to_have_text("Scan progress unavailable", timeout=6000)
    assert page.locator("#status-bar-fill.indeterminate").count() == 0
    assert "Scanning" not in page.locator("#status-bar-wrap").inner_text()
    before = p.count("GET", "/api/scan/progress")
    page.wait_for_timeout(7000)
    # Backing off 2 s -> 4 s -> 8 s: at most two more tries in 7 s (a fixed
    # 2 s poll would have made three or four).
    assert p.count("GET", "/api/scan/progress") - before <= 2
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        shot(page, f"progress-unavailable-{size[0]}")

    server.jf.tasks_status = 200
    server.jf.running = False
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")   # user comes back
    expect(page.locator("#status-bar-label")).to_have_text("Scan complete", timeout=6000)


LOST_MSG = "A library scan may still be running."


def test_lost_progress_on_a_running_scan_keeps_the_button_unavailable(server, pages):
    # A scan from another source is being followed; then Jellyfin, busy with
    # that very scan, stops answering /ScheduledTasks in time. Not knowing is
    # not "idle": a press now would restart the scan from 0 %.
    server.jf.running, server.jf.percent = True, 48.0
    p = pages(PHONE)
    page = p.open()
    btn = page.locator("#scan-btn")
    expect(page.locator("#status-bar-pct")).to_have_text("48.0%")
    expect(btn).to_have_attribute("aria-disabled", "true")
    server.jf.tasks_status = 500
    expect(page.locator("#status-bar-label")).to_have_text("Scan progress unavailable", timeout=8000)
    expect(page.locator("#status-msg")).to_have_text(LOST_MSG)
    assert page.locator("#status-msg.error").count() == 0
    expect(btn).to_have_attribute("aria-disabled", "true")
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        assert no_horizontal_scroll(page)
        shot(page, f"progress-lost-{size[0]}")
    btn.click(force=True)                                  # guarded: nothing is sent
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    assert p.count("POST", "/api/scan") == 0
    assert server.jf.refresh_calls == []
    assert server.jf.percent == 48.0                       # the running scan was left alone

    # Jellyfin answers again, the scan still going: back to following it.
    server.jf.tasks_status = 200
    server.jf.percent = 61.0
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#status-bar-pct")).to_have_text("61.0%")
    expect(page.locator("#status-msg")).to_have_text("A library scan is running.")
    expect(btn).to_have_attribute("aria-disabled", "true")
    server.jf.running = False
    expect(page.locator("#status-bar-label")).to_have_text("Scan complete", timeout=6000)
    expect(page.locator("#status-msg")).to_have_text("Ready to scan.")
    expect(btn).to_have_attribute("aria-disabled", "false")


def test_lost_progress_after_a_409_keeps_the_button_unavailable(server, pages):
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    server.jf.running, server.jf.percent = True, 12.0
    page.click("#scan-btn")                                   # -> 409, now following it
    expect(page.locator("#status-msg")).to_have_text("A library scan is already running.")
    server.jf.tasks_status = 500
    expect(page.locator("#status-bar-label")).to_have_text("Scan progress unavailable", timeout=8000)
    expect(page.locator("#status-msg")).to_have_text(LOST_MSG)
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "true")
    page.locator("#scan-btn").click(force=True)
    page.wait_for_timeout(300)
    assert p.count("POST", "/api/scan") == 1                  # only the 409 press
    assert server.jf.refresh_calls == []


def test_lost_progress_guard_lapses_after_a_few_minutes(server, pages):
    # A Jellyfin that never answers again must not lock the button forever.
    server.jf.running, server.jf.percent = True, 48.0
    # Reduced motion keeps the jellyfish still: under the fake clock the software-rendered WebGL field starves the page's timers.
    p = pages(reduced_motion=True)
    p.page.clock.install()
    page = p.open()
    btn = page.locator("#scan-btn")
    expect(page.locator("#status-bar-pct")).to_have_text("48.0%")
    server.jf.tasks_status = 500
    expect(page.locator("#status-msg")).to_have_text(LOST_MSG, timeout=8000)
    page.clock.fast_forward("02:00")
    page.wait_for_timeout(300)
    expect(btn).to_have_attribute("aria-disabled", "true")    # still guarded at 2 minutes
    page.clock.fast_forward("03:30")
    expect(btn).to_have_attribute("aria-disabled", "false", timeout=6000)
    expect(page.locator("#status-bar-label")).to_have_text("Scan progress unavailable")
    expect(page.locator("#status-msg")).to_have_text("A new scan may restart one still running.")


def test_one_missed_progress_read_is_not_an_outage(server, pages):
    # Jellyfin is often slow while it scans, and the server gives up on it
    # after 5 s. A read that fails between two good ones must not flip the
    # console to "unavailable" and back every 2 s: the status line is a live
    # region, so each flip is read out.
    server.jf.running, server.jf.percent = True, 40.0
    # Reduced motion keeps the jellyfish still: this test counts the console's 2 s polls, which the software-rendered WebGL field can starve.
    p = pages(init_script=CONSOLE_FRAMES, reduced_motion=True)
    page = p.open()
    btn = page.locator("#scan-btn")
    expect(page.locator("#status-bar-pct")).to_have_text("40.0%")
    expect(page.locator("#status-msg")).to_have_text("A library scan is running.")
    page.evaluate("window.__frames = []")
    server.jf.flap = True                            # every other answer fails from now on
    page.wait_for_timeout(9000)
    assert server.jf.flap_failures >= 2, server.jf.flap_failures
    frames = page.evaluate("window.__frames")
    assert all("unavailable" not in f and LOST_MSG not in f for f in frames), frames
    expect(page.locator("#status-msg")).to_have_text("A library scan is running.")
    expect(page.locator("#status-bar-pct")).to_have_text("40.0%")
    expect(btn).to_have_attribute("aria-disabled", "true")
    # Reads that keep failing are an outage, and then it says so.
    server.jf.flap = False
    server.jf.tasks_status = 500
    expect(page.locator("#status-bar-label")).to_have_text("Scan progress unavailable", timeout=8000)
    expect(page.locator("#status-msg")).to_have_text(LOST_MSG)
    expect(btn).to_have_attribute("aria-disabled", "true")


# Every distinct (bar label | status line | button state | countdown | live
# region) the scan console shows, plus every scan:cooldown event.
CONSOLE_FRAMES = """
    window.__frames = [];
    window.__cooldownEvents = [];
    document.addEventListener('scan:cooldown', (e) => window.__cooldownEvents.push(e.detail.until));
    new MutationObserver(() => {
      const $ = (id) => document.getElementById(id);
      if (!$('scan-btn') || !$('timer-wrap')) return;
      const bar = $('status-bar-wrap').style.display === 'flex' ? $('status-bar-label').textContent : '';
      const timer = getComputedStyle($('timer-wrap')).display === 'none' ? '' : $('timer').textContent;
      const f = [bar, $('status-msg').textContent, $('scan-btn').getAttribute('aria-disabled'),
                 timer, $('sr-announce').textContent].join(' | ');
      if (window.__frames[window.__frames.length - 1] !== f) window.__frames.push(f);
    }).observe(document, {subtree: true, childList: true, characterData: true, attributes: true});
"""


def frames_from(page, needle):
    frames = page.evaluate("window.__frames")
    at = next(i for i, f in enumerate(frames) if needle in f)
    return frames[at:]


def test_a_state_answer_from_before_a_press_is_dropped(server, pages, monkeypatch):
    # The server works out "no cooldown" for a re-sync, then is slow to send
    # it; meanwhile the user presses and the scan starts. The late answer is
    # about the world before the press and must not end the new cooldown.
    gate = Gate()
    real = app_module.cooldown_remaining
    monkeypatch.setattr(app_module, "cooldown_remaining", lambda: gate.pass_through(real()))
    p = pages(init_script=CONSOLE_FRAMES)
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    page.wait_for_timeout(300)
    gate.arm()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")   # re-sync goes out
    assert gate.held.wait(5)
    page.click("#scan-btn")
    expect(page.locator("#status-msg")).to_have_text("Scan started.")
    expect(page.locator("#timer-wrap")).to_be_visible()
    page.evaluate("window.__cooldownEvents = []")
    gate.release()                                          # the stale "no cooldown" lands now
    page.wait_for_timeout(2000)
    after = frames_from(page, "Scan started.")
    assert all("00:00" not in f for f in after), after
    # Announced once; after that the live region may only clear itself (a few
    # seconds on), never be rewritten by the stale answer's "no cooldown".
    said = [f.split(" | ")[4] for f in after]
    assert said[0] == "Scan cooldown active. Next scan available in about 60 minutes.", after
    assert set(said) <= {said[0], ""}, after
    assert 0 not in page.evaluate("window.__cooldownEvents")
    expect(page.locator("#timer")).to_have_text(re.compile(r"^59:\d\d$"))
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "true")


def test_a_progress_answer_from_before_a_press_is_dropped(server, pages):
    # A progress poll that found Jellyfin idle is slow to come back; meanwhile
    # the scheduled scan starts and the user's press gets a 409. The late
    # "idle" must not end the scan the 409 just reported.
    p = pages(init_script=CONSOLE_FRAMES)
    page = p.open()
    btn = page.locator("#scan-btn")
    expect(btn).to_have_attribute("aria-disabled", "false")
    page.wait_for_timeout(300)
    server.jf.tasks_gate.arm()
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")   # progress poll goes out
    assert server.jf.tasks_gate.held.wait(5)
    server.jf.running, server.jf.percent = True, 20.0         # the scheduled scan starts
    page.click("#scan-btn")
    expect(page.locator("#status-msg")).to_have_text("A library scan is already running.")
    server.jf.tasks_gate.release()                            # the stale "idle" lands now
    expect(page.locator("#status-bar-pct")).to_have_text("20.0%", timeout=6000)
    page.wait_for_timeout(1000)
    after = frames_from(page, "A library scan is already running.")
    assert all("Scan complete" not in f and "Ready to scan." not in f for f in after), after
    assert all(f.split(" | ")[2] == "true" for f in after), after
    assert server.jf.refresh_calls == []


# How a watched scan ended: /api/scan/progress's idle answer carries Jellyfin's
# LastExecutionResult as "last". Only a Completed run may be called complete.


def ended(status, start="2026-09-26T10:00:00.0000000Z", end="2026-09-26T10:04:30.0000000Z"):
    return {"StartTimeUtc": start, "EndTimeUtc": end, "Status": status}


@pytest.mark.parametrize("status, pressed, text, kind", [
    ("Completed", True, "Scan complete.", ""),
    ("Cancelled", False, "Scan cancelled.", ""),
    ("Failed", True, "Scan ended: Failed.", "error"),
    ("Aborted", False, "Scan ended: Aborted.", "error"),
])
def test_a_watched_scan_says_how_it_ended(server, pages, status, pressed, text, kind):
    if not pressed:                                  # someone else's scan, already running
        server.jf.running, server.jf.percent = True, 40.0
    # Reduced motion keeps the jellyfish still: this test waits out the console's 3 s "complete" beat, which the software-rendered WebGL field can starve.
    p = pages(init_script=CONSOLE_FRAMES, reduced_motion=True)
    page = p.open()
    btn = page.locator("#scan-btn")
    msg = page.locator("#status-msg")
    if pressed:
        expect(btn).to_have_attribute("aria-disabled", "false")
        page.click("#scan-btn")
        expect(msg).to_have_text("Scan started.")
    expect(page.locator("#status-bar-label")).to_have_text("Scan Media Library", timeout=6000)
    server.jf.last = ended(status)                   # Jellyfin records the result...
    server.jf.running = False                        # ...and the task goes idle
    expect(msg).to_have_text(text, timeout=6000)
    assert msg.get_attribute("class") == kind
    page.wait_for_timeout(3500)                      # past the 3 s "complete" beat
    expect(msg).to_have_text(text)                   # the line keeps saying how it ended
    expect(btn).to_have_attribute("aria-disabled", "true" if pressed else "false")
    frames = page.evaluate("window.__frames")
    if status == "Completed":
        assert any(f.startswith("Scan complete | Scan complete.") for f in frames), frames
        return
    assert all("complete" not in f.lower() for f in frames), frames     # never, not even for a frame
    expect(page.locator("#status-bar-wrap")).to_be_hidden()
    if pressed:
        expect(page.locator("#timer-wrap")).to_be_visible()             # the cooldown stands
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        assert no_horizontal_scroll(page)
        shot(page, f"ended-{status.lower()}-{size[0]}")


def test_an_earlier_runs_result_is_not_taken_for_the_watched_one(server, pages):
    # Jellyfin marks the task Idle a moment before it records how the run
    # ended, so an idle answer can still carry the previous run's result. That
    # says nothing about this run: the page falls back to its old wording.
    server.jf.last = ended("Failed", start="2026-09-26T08:00:00.0000000Z", end="2026-09-26T08:01:00.0000000Z")
    p = pages(init_script=CONSOLE_FRAMES)
    with p.page.expect_response(lambda r: r.url.split("?")[0].endswith("/api/scan/progress")):
        page = p.open()                              # the page sees that result while idle
    btn = page.locator("#scan-btn")
    expect(btn).to_have_attribute("aria-disabled", "false")
    page.wait_for_timeout(300)
    page.click("#scan-btn")
    expect(page.locator("#status-bar-label")).to_have_text("Scan Media Library", timeout=6000)
    server.jf.running = False                        # idle, this run's result not recorded yet
    expect(page.locator("#status-bar-label")).to_have_text("Scan complete", timeout=6000)
    expect(page.locator("#status-msg")).to_have_text("Scan complete.")
    assert all("Scan ended" not in f for f in page.evaluate("window.__frames"))


@pytest.mark.parametrize("status, text", [
    ("Completed", "Scan complete."),
    ("Failed", "Scan ended: Failed."),
])
def test_a_scan_over_before_the_first_poll_is_still_reported(server, pages, status, text):
    # A small library scans in a second or two: the run can start and end
    # between two polls, so the page never sees it Running. A result that
    # changed since the press is this press's run.
    server.jf.start_on_refresh = False
    server.jf.finish_on_refresh = ended(status)
    # Reduced motion keeps the jellyfish still: this test counts the console's polls, which the software-rendered WebGL field can starve.
    p = pages(init_script=CONSOLE_FRAMES, reduced_motion=True)
    with p.page.expect_response(lambda r: r.url.split("?")[0].endswith("/api/scan/progress")):
        page = p.open()                              # the result from before the press
    btn = page.locator("#scan-btn")
    expect(btn).to_have_attribute("aria-disabled", "false")
    page.click("#scan-btn")
    # Well inside the 15 s it would otherwise spend "Starting scan…".
    expect(page.locator("#status-msg")).to_have_text(text, timeout=6000)
    expect(page.locator("#timer-wrap")).to_be_visible()             # the cooldown stands
    expect(btn).to_have_attribute("aria-disabled", "true")
    frames = page.evaluate("window.__frames")
    if status == "Completed":
        assert any(f.startswith("Scan complete | Scan complete.") for f in frames), frames
    expect(page.locator("#status-bar-wrap")).to_be_hidden(timeout=6000)
    polls = p.count("GET", "/api/scan/progress")
    page.wait_for_timeout(4500)
    assert p.count("GET", "/api/scan/progress") == polls            # no longer tracking


def test_reload_after_finished_scan_shows_no_scanning(server, pages):
    server.seed([row(600, "started")])          # 50 minutes of cooldown left, Jellyfin idle
    # Reduced motion keeps the jellyfish still: this test waits out the console's polls, which the software-rendered WebGL field can starve.
    p = pages(reduced_motion=True, init_script="""
        window.__bar = [];
        new MutationObserver(() => {
          const w = document.getElementById('status-bar-wrap');
          if (w && w.style.display && w.style.display !== 'none')
            window.__bar.push(document.getElementById('status-bar-label').textContent);
        }).observe(document, {subtree: true, attributes: true, childList: true, characterData: true});
    """)
    page = p.open()
    expect(page.locator("#timer-wrap")).to_be_visible()
    expect(page.locator("#timer")).to_have_text(re.compile(r"^(49|50):\d\d$"))
    page.wait_for_timeout(3500)
    assert page.evaluate("window.__bar") == []
    expect(page.locator("#status-bar-wrap")).to_be_hidden()
    assert page.locator("#status-msg").inner_text() == ""


def test_cooldown_end_while_a_scan_still_runs_keeps_tracking(server, pages):
    server.seed([row(3600 - 3, "started")])     # cooldown ends in ~3 s
    server.jf.running, server.jf.percent = True, 64.0
    # Reduced motion keeps the jellyfish still: this test times the countdown's end, which the software-rendered WebGL field can starve.
    p = pages(reduced_motion=True)
    page = p.open()
    expect(page.locator("#status-bar-pct")).to_have_text("64.0%")
    expect(page.locator("#sr-announce")).to_have_text(
        "Scan cooldown active. Next scan available in less than a minute.")
    expect(page.locator("#timer-wrap")).to_be_hidden(timeout=8000)
    # Cooldown over, scan not: still tracked, still unavailable, and the screen
    # reader text no longer claims a cooldown.
    expect(page.locator("#status-bar-pct")).to_have_text("64.0%")
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "true")
    expect(page.locator("#status-msg")).to_have_text("A library scan is running.")
    assert page.locator("#sr-announce").inner_text() == ""
    assert page.locator("#scan-when").inner_text() == ""

    server.jf.running = False
    expect(page.locator("#status-msg")).to_have_text("Ready to scan.", timeout=6000)
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")


def test_error_colour_never_sticks_to_the_next_message(server, pages):
    server.jf.refresh_status = 500
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    page.click("#scan-btn")
    msg = page.locator("#status-msg")
    expect(msg).to_have_class("error")
    text = msg.inner_text()
    assert text and not text.startswith("Error:")            # the server's reason, as sent
    assert "jellyfin.test" not in text                        # never the internal URL
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        shot(page, f"error-{size[0]}")

    server.jf.refresh_status = 204
    page.click("#scan-btn")
    expect(msg).to_have_text("Scan started.")
    assert msg.get_attribute("class") == ""


def test_proxy_error_page_gives_a_friendly_message(server, pages):
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    page.route("**/api/scan", lambda route: route.fulfill(
        status=502, content_type="text/html", body="<!DOCTYPE html><h1>Bad gateway</h1>"))
    page.click("#scan-btn")
    expect(page.locator("#status-msg")).to_have_text(
        "Couldn't reach Jellyfin Manager (HTTP 502). Try again shortly.")


def test_hidden_tab_stops_polling(server, pages):
    server.jf.running, server.jf.percent = True, 30.0
    # Reduced motion keeps the jellyfish still: this test counts the console's polls, which the software-rendered WebGL field can starve.
    p = pages(init_script="""
        window.__hidden = false;
        Object.defineProperty(document, 'hidden', { get: () => window.__hidden });
    """, reduced_motion=True)
    page = p.open()
    expect(page.locator("#status-bar-pct")).to_have_text("30.0%")
    page.evaluate("window.__hidden = true; document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(300)
    before = p.count("GET", "/api/scan/progress")
    page.wait_for_timeout(4500)                 # a visible tab would poll twice in this time
    assert p.count("GET", "/api/scan/progress") == before
    server.jf.percent = 55.0
    page.evaluate("window.__hidden = false; document.dispatchEvent(new Event('visibilitychange'))")
    # Only the resume can do this: every loop stopped while hidden.
    expect(page.locator("#status-bar-pct")).to_have_text("55.0%", timeout=5000)


def test_429_after_an_ended_cooldown_paints_the_real_time(server, pages):
    server.seed([row(3600 - 2, "started")])            # this page's cooldown ends in ~2 s
    # Reduced motion keeps the jellyfish still: this test times the countdown's beats, which the software-rendered WebGL field can starve.
    p = pages(init_script=TIMER_FRAMES, reduced_motion=True)
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false", timeout=8000)
    page.wait_for_timeout(1500)                        # the 00:00 beat has hidden again
    # Someone else scanned meanwhile: the server answers 429.
    server.seed([row(5, "started"), row(3600, "started")])
    page.evaluate("window.__timer = []")
    page.click("#scan-btn")
    expect(page.locator("#timer-wrap")).to_be_visible()
    first = page.evaluate("window.__timer[0]")
    assert first.startswith("59:"), first              # never the old "00:00" first
    assert page.locator("#status-msg.error").count() == 0
    assert server.jf.refresh_calls == []


def test_the_cooldown_announcement_is_said_once_then_cleared(server, pages):
    # Left in place, the live region would still say "about 41 minutes" half
    # an hour later, right next to #scan-when's current value.
    server.seed([row(20 * 60 - 5, "started")])        # 40 min 5 s of cooldown left
    # Reduced motion keeps the jellyfish still: under the fake clock the software-rendered WebGL field starves the page's timers.
    p = pages(reduced_motion=True)
    p.page.clock.install()
    page = p.open()
    sr = page.locator("#sr-announce")
    expect(sr).to_have_text("Scan cooldown active. Next scan available in about 41 minutes.")
    page.clock.fast_forward(8000)
    expect(sr).to_have_text("")
    expect(page.locator("#scan-when")).to_have_text(
        re.compile(r"^Next scan available in about 4[01] minutes\.$"))
    expect(page.locator("#timer-wrap")).to_be_visible()


def test_a_429_is_not_announced_twice(server, pages):
    # The status line already says "Scan cooldown active": the other live
    # region adds only when, not the same news again.
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    server.seed([row(5, "started")])                  # someone else scanned meanwhile
    page.click("#scan-btn")
    expect(page.locator("#timer-wrap")).to_be_visible()
    expect(page.locator("#status-msg")).to_have_text("Scan cooldown active")
    expect(page.locator("#sr-announce")).to_have_text("Next scan available in about 60 minutes.")
    assert server.jf.refresh_calls == []


def test_another_users_press_shows_up_when_the_tab_comes_back(server, pages):
    bob = pages()
    bob_page = bob.open()
    expect(bob_page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    alice = pages()
    alice_page = alice.open()
    expect(alice_page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    alice_page.click("#scan-btn")
    expect(alice_page.locator("#status-msg")).to_have_text("Scan started.")
    # Bob's tab regains focus: it re-syncs at once instead of offering a press
    # that would only log a spurious "cooldown" row under his name.
    bob_page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(bob_page.locator("#timer-wrap")).to_be_visible(timeout=4000)
    expect(bob_page.locator("#scan-btn")).to_have_attribute("aria-disabled", "true")
    expect(bob_page.locator("#status-bar-label")).to_have_text("Scan Media Library", timeout=4000)
    expect(bob_page.locator(".history-row .badge.started")).to_have_count(1, timeout=4000)
    assert bob.count("POST", "/api/scan") == 0


def test_slow_jellyfin_never_gets_overlapping_progress_polls(server, pages):
    server.jf.running, server.jf.percent = True, 50.0
    server.jf.tasks_delay = 3.0
    # Reduced motion keeps the jellyfish still: this test counts the console's polls, which the software-rendered WebGL field can starve.
    p = pages(reduced_motion=True)
    page = p.open()
    expect(page.locator("#status-bar-pct")).to_have_text("50.0%", timeout=8000)
    page.wait_for_timeout(9000)
    assert server.jf.max_inflight == 1
    # One request at a time, each followed by a 2 s pause: ~5 s per round.
    assert p.count("GET", "/api/scan/progress") <= 4


def test_relative_times_refresh_while_the_page_stays_open(server, pages):
    server.seed([row(5, "cooldown")])
    # Reduced motion keeps the jellyfish still: under the fake clock the software-rendered WebGL field starves the page's timers.
    p = pages(reduced_motion=True)
    p.page.clock.install()
    page = p.open()
    rel = page.locator(".history-rel").first
    expect(rel).to_have_text("just now")
    page.clock.fast_forward("02:00")                   # fires the 60 s refresh once
    expect(rel).to_have_text("2 minutes ago", timeout=6000)


def test_lockout_countdown_follows_the_wall_clock(server, pages):
    server.state["auth"] = False
    server.jf.auth_status = 401
    # A wall clock the test can move without running any timers (what a
    # sleeping laptop or a throttled background tab looks like to the page).
    # Reduced motion keeps the jellyfish still: this test times the countdown's ticks, which the software-rendered WebGL field can starve.
    p = pages(signed_in=False, init_script="""
        const realNow = Date.now.bind(Date);
        window.__skew = 0;
        Date.now = () => realNow() + window.__skew;
    """, reduced_motion=True)
    page = p.open("/login")
    for _ in range(3):
        page.fill("#username", "alice")
        page.fill("#password", "wrong")
        page.click("button[type=submit]")
    timer = page.locator("#timer")
    expect(timer).to_have_text(re.compile(r"^(60:00|59:\d\d)$"))
    page.evaluate("window.__skew = 30 * 60 * 1000")
    expect(timer).to_have_text(re.compile(r"^(30:00|29:\d\d)$"), timeout=4000)
    # Past the deadline: the page reloads itself (the server still says locked,
    # so the reloaded page shows the lock again, from real time).
    with page.expect_navigation(timeout=6000):
        page.evaluate("window.__skew = 61 * 60 * 1000")
    expect(timer).to_have_text(re.compile(r"^(60:00|59:\d\d)$"))


# ---------------------------------------------------------------- dive log


def test_busy_badge_and_coalesced_count(server, pages):
    server.seed([
        row(90, "busy"),
        row(4000, "cooldown", count=3, last_ts=time.time() - 3900),
        row(5000, "error", error="Jellyfin unreachable"),
    ])
    p = pages()
    page = p.open()
    expect(page.locator(".history-row")).to_have_count(3)
    busy = page.locator(".history-row").nth(0).locator(".badge")
    expect(busy).to_have_class("badge busy")
    expect(busy).to_have_text("busy")
    colours = page.evaluate("""() => {
      const b = document.querySelector('.badge.busy');
      const u = document.createElement('span'); u.className = 'badge unknown';
      document.body.appendChild(u);
      const cb = getComputedStyle(b), cu = getComputedStyle(u);
      return [cb.borderTopColor, cu.borderTopColor, cb.color];
    }""")
    assert colours[0] != colours[1]                      # distinct from "unknown"
    times = page.locator(".history-row").nth(1).locator(".history-times")
    expect(times).to_contain_text("×3")
    assert "3 presses" in times.get_attribute("title")
    assert page.locator(".history-row").nth(0).locator(".history-times").count() == 0
    expect(page.locator(".history-err")).to_have_text("Jellyfin unreachable")
    # Three rows, five presses.
    expect(page.locator("#history-count")).to_have_text("5 presses")


NOTE = "Jellyfin was slow to answer; the scan was probably started"
CORAL = "rgb(255, 107, 122)"
BENTHOS = "rgb(124, 147, 184)"


def test_a_note_on_a_started_row_is_not_painted_as_an_error(server, pages):
    # The Refresh left but its answer was lost: a probable start, recorded as
    # "started" with a note. The note is information, not a failure.
    server.seed([row(5000, "error", error="Jellyfin unreachable")])
    server.jf.refresh_exc = app_module.requests.ReadTimeout("read timed out")
    p = pages()
    page = p.open()
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    page.click("#scan-btn")
    expect(page.locator("#status-msg")).to_have_text("Scan started.")
    rows = page.locator(".history-row")
    expect(rows).to_have_count(2)
    started, failed = rows.nth(0), rows.nth(1)
    expect(started.locator(".badge")).to_have_text("started")
    expect(started.locator(".history-note")).to_have_text(NOTE)
    assert started.locator(".history-err").count() == 0
    expect(failed.locator(".history-err")).to_have_text("Jellyfin unreachable")
    assert failed.locator(".history-note").count() == 0
    colour = "(sel) => getComputedStyle(document.querySelector(sel)).color"
    assert page.evaluate(colour, ".history-note") == BENTHOS
    assert page.evaluate(colour, ".history-err") == CORAL
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        assert no_horizontal_scroll(page)
        page.locator("#history-list").scroll_into_view_if_needed()
        shot(page, f"history-note-{size[0]}")


UNSAVED = "History isn\u2019t being saved \u2014 check the /data volume."


def test_unsaved_history_is_flagged_in_the_dive_log(server, pages, monkeypatch):
    server.seed([row(5000, "cooldown"), row(6000, "busy")])
    monkeypatch.setattr(app_module, "_history_writable", False)
    p = pages()
    page = p.open()
    warn = page.locator("#history-unsaved")
    expect(page.locator(".history-row")).to_have_count(2)
    expect(warn).to_be_visible()
    expect(warn).to_have_text(UNSAVED)
    assert page.evaluate("getComputedStyle(document.getElementById('history-unsaved')).color") == CORAL
    expect(page.locator("#history-error")).to_be_hidden()
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        assert no_horizontal_scroll(page)
        shot(page, f"history-unsaved-{size[0]}")

    # A later save worked (the volume was fixed): the next answer clears it.
    monkeypatch.setattr(app_module, "_history_writable", True)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(warn).to_be_hidden()

    # A broken /data usually breaks reads too. The 503 carries the flag, and
    # it is often the only answer the page gets while the warning matters most.
    def unreadable(*args, **kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr(app_module, "_history_writable", False)
    monkeypatch.setattr(app_module.history, "entries", unreadable)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.locator("#history-error")).to_be_visible()
    expect(warn).to_be_visible()
    expect(page.locator(".history-row")).to_have_count(2)          # what was shown stays
    for size in (DESKTOP, PHONE):
        page.set_viewport_size({"width": size[0], "height": size[1]})
        page.wait_for_timeout(200)
        shot(page, f"history-unsaved-503-{size[0]}")

    # Still unreadable, but saving works again: the 503 clears it too.
    monkeypatch.setattr(app_module, "_history_writable", True)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    expect(warn).to_be_hidden()
    expect(page.locator("#history-error")).to_be_visible()


def test_show_older_appends(server, pages):
    server.seed([row(7200 + i * 60, "cooldown", user=f"user{i}") for i in range(180)])
    p = pages()
    page = p.open()
    rows = page.locator(".history-row")
    expect(rows).to_have_count(50)
    expect(page.locator("#history-count")).to_have_text("Latest 50 of 180")
    page.evaluate("document.querySelector('.history-row').__marker = 1")
    more = page.get_by_role("button", name="Show older")
    more.click()
    expect(rows).to_have_count(100)
    expect(page.locator("#history-count")).to_have_text("Latest 100 of 180")
    # Appended, not replaced: the first row is the very same node.
    assert page.evaluate("document.querySelector('.history-row').__marker") == 1
    assert rows.nth(50).locator(".history-user").inner_text() == "user50"
    assert p.count("GET", "/api/history") >= 2
    assert any("offset=50" in u for _, u in p.requests)
    more.focus()
    page.keyboard.press("Enter")
    expect(rows).to_have_count(150)
    page.keyboard.press("Enter")
    expect(rows).to_have_count(180)
    expect(page.locator("#history-count")).to_have_text("180 presses")
    expect(more).to_be_hidden()
    assert page.evaluate("document.activeElement.id") == "history-title"   # focus kept on the log
    names = page.locator(".history-user").all_inner_texts()
    assert len(set(names)) == 180


@pytest.mark.parametrize("size", [PHONE, DESKTOP, (320, 700)])
def test_ipv6_row_keeps_the_username_visible(server, pages, size):
    server.seed([
        row(120, "cooldown", user="grandma-livingroom", ip=IPV6, user_agent=UA_CHROME_ANDROID),
        row(180, "cooldown", user="dad", ip="198.51.100.23"),
    ])
    p = pages(size)
    page = p.open()
    expect(page.locator(".history-row")).to_have_count(2)
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")
    m = page.evaluate("""() => {
      const panel = document.querySelector('#history-list').closest('.panel').getBoundingClientRect();
      return [...document.querySelectorAll('.history-row')].map((row) => {
        const box = (sel) => {
          const n = row.querySelector(sel), r = n.getBoundingClientRect();
          return {w: n.clientWidth, sw: n.scrollWidth, left: r.left, right: r.right};
        };
        return {user: box('.history-user'), ip: box('.history-ip'), ua: box('.history-ua'),
                panelLeft: panel.left, panelRight: panel.right};
      });
    }""")
    for r in m:
        assert r["user"]["w"] > 0 and r["user"]["sw"] <= r["user"]["w"], r      # whole name shown
        for part in ("user", "ip", "ua"):
            assert r["panelLeft"] <= r[part]["left"] and r[part]["right"] <= r["panelRight"], (part, r)
    ipv6 = m[0]["ip"]
    if size[0] >= 390:
        assert ipv6["sw"] <= ipv6["w"], ipv6                                   # the whole address too
    assert no_horizontal_scroll(page)
    if size == PHONE:
        shot(page, "history-ipv6-390")


# ---------------------------------------------------------------- login page


def test_login_error_is_an_alert_and_keeps_the_username(server, pages):
    server.state["auth"] = False
    server.jf.auth_status = 401
    for size in (DESKTOP, PHONE):
        p = pages(size, signed_in=False)
        page = p.open("/login")
        shot(page, f"login-{size[0]}")
        page.fill("#username", "alice")
        page.fill("#password", "wrong-password")
        page.click("button[type=submit]")
        err = page.locator("#login-error")
        expect(err).to_be_visible()
        assert err.get_attribute("role") == "alert"
        assert err.inner_text().strip()
        expect(page.locator("#username")).to_have_value("alice")
        expect(page.locator("#password")).to_have_value("")
        for field in ("#username", "#password"):
            assert page.locator(field).get_attribute("aria-invalid") == "true"
            assert page.locator(field).get_attribute("aria-describedby") == "login-error"
        assert page.evaluate("document.activeElement.id") == "password"
        assert no_horizontal_scroll(page)
        page.wait_for_timeout(600)       # let the shake settle before the picture
        shot(page, f"login-error-{size[0]}")
