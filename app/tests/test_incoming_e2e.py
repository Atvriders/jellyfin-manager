"""Browser checks for the Incoming panel (real Chrome via playwright).

Skipped when playwright or a browser isn't available (e.g. CI, which only
installs requirements + pytest). The backend is covered by test_downloads.py;
this file checks what actually renders.

Set INCOMING_SHOTS=/some/dir to also save screenshots of each state at phone
and desktop width for a human to look at.
"""

import json
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
            # The out-of-space self-lock: qBittorrent reports progress just
            # over 1 (amount_left < 0); the server clamps it to 1.0.
            {"source": "Box 1", "name": "Stuck.Torrent.2025", "progress": 1.0,
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
        page.mouse.move(0, 0)               # resting state, not a hover
        page.wait_for_timeout(400)          # let 0.15 s colour transitions settle
        page.screenshot(path=os.path.join(target, name + ".png"), full_page=True)


BENTHOS = "rgb(124, 147, 184)"


def no_horizontal_scroll(page):
    return page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


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
    page.wait_for_url("**/login", timeout=20_000)
    page.close()


def test_counts_fit_the_panel_at_320(server, browser):
    url, stub, _ = server
    stub.body = body(downloading=[
        {"source": "Box 1", "name": f"Show.{i}", "progress": 0.1 * (i % 9), "state": "downloading",
         "dlspeed": 1000, "eta": 600, "size": 1} for i in range(11)], downloading_total=11)
    page = open_page(browser, url, 320, 640)
    count = page.locator("#incoming-count")
    assert count.text_content().replace("\u00a0", " ") == "11 downloading · 2 ready"
    m = page.evaluate("""() => {
      const panel = document.querySelector('#incoming .panel');
      const cs = getComputedStyle(panel), box = panel.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(document.getElementById('incoming-count'));
      const text = range.getBoundingClientRect();
      return {text: text.right, edge: box.right - parseFloat(cs.borderRightWidth) - parseFloat(cs.paddingRight),
              lines: range.getClientRects().length};
    }""")
    assert m["text"] <= m["edge"] + 0.5, m                      # inside the panel's padding
    assert no_horizontal_scroll(page)
    shot(page, "incoming-counts-320")
    page.close()


def test_paused_rows_keep_their_text_at_full_strength(server, browser):
    # Row-wide opacity took the tag and the percent to 2.64:1. The bar and
    # the name step back; the small text keeps its 4.5:1.
    url, _, _ = server
    page = open_page(browser, url)
    m = page.evaluate("""() => {
      const row = [...document.querySelectorAll('#incoming-downloading .dl-row')]
        .find((r) => r.classList.contains('is-paused'));
      const eff = (n) => { let o = 1; for (; n; n = n.parentElement) o *= parseFloat(getComputedStyle(n).opacity); return o; };
      const q = (sel) => row.querySelector(sel);
      return {meta: eff(q('.dl-meta')), tag: eff(q('.src-tag')), name: eff(q('.dl-name')),
              track: eff(q('.dl-track')), metaColor: getComputedStyle(q('.dl-meta')).color,
              nameColor: getComputedStyle(q('.dl-name')).color};
    }""")
    assert m["meta"] == 1 and m["tag"] == 1 and m["name"] == 1, m
    assert m["track"] < 1, m
    assert m["metaColor"] == BENTHOS and m["nameColor"] == BENTHOS, m
    page.close()


def test_one_box_down_with_nothing_listed_never_says_nothing(server, browser):
    # "Unknown is never zero": Box 2 couldn't be asked, so the panel only
    # speaks for the box that answered.
    url, stub, _ = server
    stub.body = body(downloading=[], downloading_total=0, ready=[],
                     sources=[{"label": "Box 1", "ok": True},
                              {"label": "Box 2", "ok": False, "error": "unreachable"}])
    page = open_page(browser, url, 390, 844)
    assert visible_text(page, ".src-problem") == ["Box 2: unreachable"]
    expect(page.locator("#incoming-downloading-empty")).to_have_text("Nothing downloading on Box 1.")
    expect(page.locator("#incoming-ready-empty")).to_have_text("Nothing from Box 1 waiting for a scan.")
    text = page.locator("#incoming").inner_text()
    assert "Nothing downloading right now." not in text and "Nothing waiting for a scan." not in text
    assert not page.locator("#incoming-downloading-unknown").is_visible()
    shot(page, "incoming-one-down-empty-390")
    page.close()


LAST_BEFORE = {"status": "Completed", "started_at": NOW_S - 86_400, "ended_at": NOW_S - 86_100}
LAST_AFTER = {"status": "Completed", "started_at": NOW_S + 5, "ended_at": NOW_S + 25}


def test_the_nudge_follows_the_scan_console(server, browser):
    # The panel's own answer can be 10 s old (the server's cache) plus 15 s
    # (its poll); the console looks every 2 s while it follows a scan. The
    # nudge must never contradict the console's line about the same scan.
    url, stub, state = server
    jf = {"progress": {"state": "idle", "percent": 0, "last": LAST_BEFORE}}
    downloads = []
    # Reduced motion keeps the jellyfish from animating. This test times the
    # console's 2 s progress polls, and the software-rendered WebGL field can
    # starve the page's timers on a loaded machine (the polls then arrive
    # seconds late and the test fails for reasons unrelated to the page).
    page = browser.new_page(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    page.clock.install(time=NOW_S * 1000)
    page.on("response", lambda r: downloads.append(r) if r.url.endswith("/api/downloads") else None)
    page.route("**/api/scan/progress", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(jf["progress"])))

    def press(route):
        state["cooldown"] = 3600.0
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "started"}))

    page.route("**/api/scan", press)
    page.goto(url)
    page.wait_for_selector("#incoming-loading", state="hidden")
    nudge = page.locator("#incoming-nudge")
    metas = "#incoming-ready .dl-meta"
    expect(nudge).to_have_text("2 finished downloads aren't in Jellyfin yet — scan to add them.")
    expect(page.locator("#scan-btn")).to_have_attribute("aria-disabled", "false")

    page.click("#scan-btn")
    # At once, not at the panel's next look.
    expect(nudge).to_have_text("Adding 2 downloads now…", timeout=1000)
    assert visible_text(page, metas) == ["adding now…", "adding now…"]
    # Jellyfin is slow to flip the task to Running: the panel's own look after
    # the press still says idle. The console is still waiting for the start.
    seen = len(downloads)
    page.wait_for_timeout(4500)
    assert len(downloads) > seen
    expect(nudge).to_have_text("Adding 2 downloads now…")

    jf["progress"] = {"state": "running", "percent": 30.0, "name": "Scan Media Library"}
    stub.body = body(scan={"running": True, "percent": 30.0},
                     ready=[dict(r, adding=True) for r in body()["ready"]])
    expect(page.locator("#status-bar-pct")).to_have_text("30.0%", timeout=6000)
    expect(nudge).to_have_text("Adding 2 downloads now…")

    # The scan ends. The server's cached answer still says it runs.
    jf["progress"] = {"state": "idle", "percent": 0, "last": LAST_AFTER}
    expect(page.locator("#status-msg")).to_have_text("Scan complete.", timeout=6000)
    expect(nudge).to_be_hidden(timeout=1000)
    assert "adding now…" not in visible_text(page, metas)
    seen = len(downloads)
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")   # a look inside that window
    page.wait_for_timeout(1000)
    assert len(downloads) > seen
    expect(nudge).to_be_hidden()
    assert "adding now…" not in visible_text(page, metas)

    # Once the server's answer has turned over, the panel looks again.
    stub.body = body(ready=[])                          # the scan added both
    page.clock.fast_forward(12_000)
    expect(page.locator("#incoming-ready-empty")).to_be_visible()
    expect(page.locator("#incoming-ready-empty")).to_have_text("Nothing waiting for a scan.")
    expect(nudge).to_be_hidden()
    page.close()
