"""Browser checks for the Jellyfield HD engine (real Chrome via playwright).

Skipped without playwright, numpy, Pillow or a browser (CI). Uses SwiftShader for WebGL.
Set JELLY_SHOTS=/dir to save frames for a human to look at.
"""

import os
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
pytest.importorskip("numpy")   # the pixel checks read frames as arrays
pytest.importorskip("PIL")

from werkzeug.serving import make_server

import app as app_module
from conftest import FakeResponse
from history import ScanHistory

GL_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
           "--enable-precise-memory-info"]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "authenticated", lambda: True)
    monkeypatch.setattr(app_module, "history", ScanHistory(str(tmp_path / "h.json")))
    monkeypatch.setattr(app_module, "JELLYFIN_URL", "http://jellyfin.test")
    monkeypatch.setattr(app_module, "JELLYFIN_API_KEY", "k")
    monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse(payload=[]))
    srv = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/"
    srv.shutdown()


def _launch(pw, extra=()):
    attempts = [{}] + [{"executable_path": p} for p in
                       (os.environ.get("CHROME_PATH"), "/usr/bin/google-chrome", "/usr/bin/chromium")
                       if p and os.path.exists(p)]
    errors = []
    for kw in attempts:
        try:
            return pw.chromium.launch(args=GL_ARGS + list(extra), **kw)
        except Exception as exc:  # pragma: no cover - environment dependent
            errors.append(str(exc).splitlines()[0])
    pytest.skip("no usable chromium: " + " | ".join(errors))


@pytest.fixture(scope="module")
def pw():
    try:
        p = sync_api.sync_playwright().start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"playwright unavailable: {exc}")
    yield p
    p.stop()


@pytest.fixture(scope="module")
def browser(pw):
    b = _launch(pw)
    yield b
    b.close()


def wait_for(page, expr, timeout=30_000):
    """page.wait_for_function, polled from here instead. The app's CSP has no
    'unsafe-eval', which blocks playwright's in-page polling predicate the
    moment a first check comes back false; page.evaluate of a plain
    expression is exempt, and the page keeps its production CSP."""
    deadline = time.monotonic() + timeout / 1000
    while not page.evaluate(expr):
        if time.monotonic() > deadline:
            raise TimeoutError(f"gave up after {timeout} ms waiting for: {expr}")
        time.sleep(0.05)


_open_contexts = []


@pytest.fixture(autouse=True)
def _close_leftover_contexts():
    """A test that fails at an assert never reaches its ctx.close(); its page
    keeps rendering on SwiftShader, and two of those saturate the shared
    browser so every later page.goto times out. Close whatever is left."""
    yield
    while _open_contexts:
        try:
            _open_contexts.pop().close()
        except Exception:
            pass


def open_page(browser, url, width=1440, height=900, dpr=1, reduced=False, query="", init_script=None):
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=dpr,
                              reduced_motion="reduce" if reduced else "no-preference")
    _open_contexts.append(ctx)
    if init_script:
        ctx.add_init_script(init_script)   # runs before any page script, e.g. to break WebGL on purpose
    page = ctx.new_page()
    errors = []

    def on_console(m):
        # The app serves no favicon; Chrome reports that 404 as a console error.
        # Only that one is ignored, so a missing engine script still counts.
        if m.type == "error" and not m.location.get("url", "").endswith("/favicon.ico"):
            errors.append(m.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + query)
    wait_for(page, "window.Jellyfield && window.Jellyfield.info && window.Jellyfield.info().engine !== ''")
    return ctx, page, errors


def info(page):
    return page.evaluate("window.Jellyfield.info()")


def wait_frames(page, n=20, timeout=20_000):
    start = info(page)["frames"] or 0
    wait_for(page, f"(window.Jellyfield.info().frames || 0) >= {start + n}", timeout)


def shot(page, name):
    d = os.environ.get("JELLY_SHOTS")
    if d:
        os.makedirs(d, exist_ok=True)
        page.screenshot(path=os.path.join(d, name + ".png"))


def test_hd_mounts_by_default_without_errors(server, browser):
    ctx, page, errors = open_page(browser, server)
    wait_frames(page)
    i = info(page)
    assert i["engine"] == "hd"
    assert i["species"] == "striped"   # the one creature: the bake-off's pick
    assert errors == []
    shot(page, "hd-default-1440")
    ctx.close()


def test_no_webgl_falls_back_to_2d(server, pw):
    b = _launch(pw, extra=["--disable-webgl", "--disable-webgl2"])
    ctx, page, errors = open_page(b, server)
    assert info(page)["engine"] == "2d"
    ctx.close()
    b.close()


def test_reduced_motion_uses_2d_and_flips_back(server, browser):
    ctx, page, errors = open_page(browser, server, reduced=True)
    assert info(page)["engine"] == "2d"
    page.emulate_media(reduced_motion="no-preference")
    wait_for(page, "window.Jellyfield.info().engine === 'hd'", 10_000)
    page.emulate_media(reduced_motion="reduce")
    wait_for(page, "window.Jellyfield.info().engine === '2d'", 10_000)
    assert page.locator("canvas#jellyfield").count() == 1
    # HD -> 2D -> HD: the same HD engine mounts a second time, on a fresh canvas
    page.emulate_media(reduced_motion="no-preference")
    wait_for(page, "window.Jellyfield.info().engine === 'hd'", 10_000)
    wait_frames(page, 5, timeout=60_000)
    assert info(page)["engine"] == "hd"
    assert page.locator("canvas#jellyfield").count() == 1
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("dpr", [1, 2, 3])
def test_drawing_buffer_matches_capped_dpr(server, browser, dpr):
    ctx, page, errors = open_page(browser, server, width=800, height=600, dpr=dpr)
    wait_frames(page, 5)
    i = info(page)
    assert (i["drawW"], i["drawH"]) == (800 * min(dpr, 3), 600 * min(dpr, 3))
    assert i["dpr"] == min(dpr, 3)
    ctx.close()


def test_4k_at_2x_respects_pixel_cap(server, browser):
    ctx, page, errors = open_page(browser, server, width=3840, height=2160, dpr=2)
    wait_frames(page, 3, timeout=60_000)
    i = info(page)
    assert i["drawW"] * i["drawH"] <= 8_294_400
    assert abs(i["drawW"] / i["drawH"] - 3840 / 2160) < 0.01
    assert abs(i["drawW"] - 3840 * i["dpr"]) <= 1   # dpr is the EFFECTIVE ratio, cap included
    ctx.close()


def test_hidden_tab_pauses_rendering(server, browser):
    ctx, page, errors = open_page(browser, server)
    wait_frames(page, 5)
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                             document.dispatchEvent(new Event('visibilitychange')); }""")
    before = info(page)["frames"]
    page.wait_for_timeout(1500)
    assert info(page)["frames"] - before <= 1
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                             document.dispatchEvent(new Event('visibilitychange')); }""")
    wait_frames(page, 5)
    ctx.close()


def _lose(page):
    return page.evaluate("""() => { const c = document.querySelector('canvas#jellyfield');
        const gl = c.getContext('webgl2'); const ext = gl.getExtension('WEBGL_lose_context');
        window.__loseExt = ext; ext.loseContext(); return true; }""")


def test_context_loss_then_restore_keeps_hd(server, browser):
    ctx, page, errors = open_page(browser, server)
    wait_frames(page, 5)
    _lose(page)
    page.wait_for_timeout(300)
    page.evaluate("window.__loseExt.restoreContext()")
    wait_frames(page, 10)
    assert info(page)["engine"] == "hd"
    assert [e for e in errors if "CONTEXT_LOST" not in e] == []
    ctx.close()


def test_context_loss_without_restore_demotes_to_2d(server, browser):
    ctx, page, errors = open_page(browser, server)
    wait_frames(page, 5)
    _lose(page)
    # (the plan's page.wait_for_function, polled from Python: the CSP has no unsafe-eval)
    wait_for(page, "window.Jellyfield.info().engine === '2d'", 8_000)
    assert page.locator("canvas#jellyfield").count() == 1
    ctx.close()


def test_context_restored_while_hidden_rebuilds_on_return(server, browser):
    ctx, page, errors = open_page(browser, server)
    wait_frames(page, 5)
    _lose(page)
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
                             document.dispatchEvent(new Event('visibilitychange'));
                             window.__loseExt.restoreContext(); }""")
    page.wait_for_timeout(500)
    page.evaluate("""() => { Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});
                             document.dispatchEvent(new Event('visibilitychange')); }""")
    wait_frames(page, 10)
    assert info(page)["engine"] == "hd"
    ctx.close()


# Makes every WebGL2 getParameter() throw. The HD engine calls it at mount
# (detectCaps), so this stands in for any failure while building resources; the
# 2D field must then win the fallback on a fresh canvas.
FORCED_BUILD_FAILURE = """(() => {
    WebGL2RenderingContext.prototype.getParameter = function () { throw new Error('forced build failure'); };
})()"""


def test_build_failure_in_mount_falls_back_cleanly(server, browser):
    ctx, page, errors = open_page(browser, server, init_script=FORCED_BUILD_FAILURE)
    i = info(page)
    assert i["engine"] == "2d"
    assert page.locator("canvas#jellyfield").count() == 1
    page.wait_for_timeout(1000)   # the fallback keeps running; nothing leaks out of the dead HD attempt
    assert page.locator("canvas#jellyfield").count() == 1
    # the released HD context is reported by Chrome as a lost context; that line is expected
    assert [e for e in errors if "CONTEXT_LOST" not in e] == []
    ctx.close()


def test_heap_is_stable_over_a_minute(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low")
    wait_frames(page, 30, timeout=60_000)
    page.wait_for_timeout(3000)
    h0 = page.evaluate("performance.memory.usedJSHeapSize")
    page.wait_for_timeout(60_000)
    h1 = page.evaluate("performance.memory.usedJSHeapSize")
    assert h1 - h0 < 2_000_000
    ctx.close()


def _canvas_rgb(page):
    import io
    from PIL import Image
    png = page.locator("canvas#jellyfield").screenshot()
    return Image.open(io.BytesIO(png)).convert("RGB")


def test_water_renders_a_banding_free_gradient(server, browser):
    import numpy as np
    ctx, page, errors = open_page(browser, server, query="?jellytier=ultra")
    wait_frames(page, 20, timeout=60_000)
    shot(page, "water-ultra-1440")
    img = np.asarray(_canvas_rgb(page)).astype(int)
    col = img[:, 40, :].sum(axis=1)   # a left-edge column: water only, no creature
    assert col.max() > 30              # not a black frame
    runs, cur = [], 1                  # dithering means no long flat runs in a smooth gradient
    for a, b in zip(col, col[1:]):
        if a == b:
            cur += 1
        else:
            runs.append(cur); cur = 1
    assert max(runs) < 40
    assert errors == []
    ctx.close()


def test_ldr_mode_renders(server, browser):
    import numpy as np
    ctx, page, errors = open_page(browser, server, query="?jellyldr=1")
    wait_frames(page, 20, timeout=60_000)
    assert info(page)["hdr"] is False
    shot(page, "water-ldr-1440")
    img = np.asarray(_canvas_rgb(page))
    assert img.mean() > 8
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("q", ["?jellydebug=1", "?jellyldr=1&jellydebug=1"])   # HDR and the sRGB8 LDR targets
def test_debug_mode_reports_no_gl_errors(server, browser, q):
    ctx, page, errors = open_page(browser, server, query=q)
    wait_frames(page, 30, timeout=60_000)
    assert [e for e in errors if "GL error" in e] == []
    ctx.close()


def test_dim_band_leaves_the_backdrop_alone(server, browser):
    """The dim band dims only the LIGHT layers behind the text column
    (silhouettes, motes, glow, shells, the creature), never the water's own gradient. Two
    water-only columns at the same rows: x=40 (outside the band) and x=170
    (inside it: .column starts at 201.6 px at 1440 wide, so the band begins at
    141.6 px, and nothing of the page is drawn left of the column). The
    backdrop's own radial falloff makes x=170 ~15% brighter than x=40; a
    dimmed backdrop would put it near 0.4x. Medians over 260 mid-height rows
    so a stray mote cannot tip it either way."""
    import numpy as np
    ctx, page, errors = open_page(browser, server)
    wait_frames(page, 20, timeout=60_000)
    img = np.asarray(_canvas_rgb(page)).astype(int)
    outside = np.median(img[320:580, 40, :].sum(axis=1))
    inside = np.median(img[320:580, 170, :].sum(axis=1))
    assert outside > 10
    assert 0.75 * outside <= inside <= 1.6 * outside, (outside, inside)
    assert errors == []
    ctx.close()


# ---- quality tiers and adaptive stepping ----
# SwiftShader renders ~3 fps at Ultra here (and ~1.4 fps on the 3x phone), so the
# tier clock (RAF-to-RAF wall time, clamped at 250 ms) ticks ~4x per second and
# the 30 warm-up frames after a tier change alone take ~10 s. The timeouts below
# are those numbers with room; on a 60 fps GPU the same waits finish in seconds.

TIER_SCALE = {"ultra": 1.0, "high": 0.85, "low": 0.7}


def _host(cores, mem):
    """An init script that fixes what the starting-tier heuristic reads from
    navigator, so an UNPINNED page starts where the test says and not where the
    machine running the test happens to fall (a <= 4-core runner starts at High
    and can never step up to Ultra)."""
    return (f"Object.defineProperty(navigator, 'hardwareConcurrency', {{configurable: true, get: () => {cores}}});"
            f"Object.defineProperty(navigator, 'deviceMemory', {{configurable: true, get: () => {mem}}});")


STRONG_HOST = _host(8, 8)   # above both thresholds: a desktop starts at Ultra
WEAK_HOST = _host(4, 8)     # at the core threshold: starts at High, whatever the width


def test_tier_can_be_pinned(server, browser):
    for t in ("ultra", "high", "low"):
        ctx, page, errors = open_page(browser, server, query=f"?jellytier={t}")
        wait_frames(page, 3, timeout=60_000)
        i = info(page)
        assert i["tier"] == t
        assert i["renderScale"] == TIER_SCALE[t]
        assert errors == []
        shot(page, f"tier-{t}-1440")
        ctx.close()


def test_phone_starts_at_high(server, browser):
    """The W <= 720 branch alone: the host is strong, so only the width can
    account for High (?jellyslow=8 reads fast, and High is the phone's ceiling)."""
    ctx, page, errors = open_page(browser, server, width=390, height=844, dpr=3, query="?jellyslow=8",
                                  init_script=STRONG_HOST)
    wait_frames(page, 3, timeout=60_000)
    assert info(page)["tier"] == "high"
    ctx.close()


def test_small_machine_starts_at_high(server, browser):
    """The hardwareConcurrency <= 4 branch alone, at desktop width."""
    ctx, page, errors = open_page(browser, server, query="?jellyslow=8", init_script=WEAK_HOST)
    wait_frames(page, 3, timeout=60_000)
    assert info(page)["tier"] == "high"
    ctx.close()


def test_slow_frames_step_down_then_recover(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellyslow=40", init_script=STRONG_HOST)
    start = info(page)["tier"]
    assert start == "ultra"
    # warm-up (30 frames), then EMA > 20 ms held for 2 s: ~14 s here
    wait_for(page, "window.Jellyfield.info().tier !== 'ultra'", 40_000)
    assert info(page)["tier"] == "high"          # one step at a time
    page.evaluate("window.__jellyfieldSlow = 5")  # the runtime override (only with ?jellyslow at load)
    # warm-up again, EMA < 11 ms held for 10 s, and at least 8 s since the change: ~25 s here
    wait_for(page, "window.Jellyfield.info().tier === 'ultra'", 90_000)
    assert errors == []
    ctx.close()


@pytest.mark.parametrize("q,slow", [("?jellyslow", None), ("?jellyslow=abc", None), ("?jellyslow=-3", None),
                                    ("?jellyslow=0", None), ("?jellyslow=8", 8)])
def test_jellyslow_param_is_guarded(server, browser, q, slow):
    """A bare or garbage ?jellyslow must read as absent (real frame times), not
    as a synthetic 0 ms / NaN frame that would pin the EMA below the step-up line."""
    ctx, page, errors = open_page(browser, server, query=q)
    assert page.evaluate("window.JellyfieldHD.params.slow") == slow
    ctx.close()


def _water_noise(img):
    """Mean |difference| between horizontally adjacent pixels (channel sum) in a
    water-only patch: rows 320-580, x 20-120 (left of the column, below the
    shafts). Dither and grain give ~1; posterized 8-bit water gives >10."""
    import numpy as np
    reg = img[320:580, 20:120, :].astype(int).sum(axis=2)
    return float(np.abs(np.diff(reg, axis=1)).mean())


def test_ldr_water_is_not_speckled(server, browser):
    """LDR targets store sRGB, so the dark water keeps its 8-bit precision in
    perceptual space; with linear RGBA8 it sat at 0-2 LSB and read as speckle."""
    import numpy as np
    ctx, page, errors = open_page(browser, server, query="?jellytier=ultra")
    wait_frames(page, 12, timeout=60_000)
    hdr = _water_noise(np.asarray(_canvas_rgb(page)))
    ctx.close()
    # unpinned, so the host is fixed: both frames are then Ultra (the HDR page pins it)
    ctx, page, errors = open_page(browser, server, query="?jellyldr=1", init_script=STRONG_HOST)
    wait_frames(page, 12, timeout=60_000)
    assert info(page)["hdr"] is False
    assert info(page)["tier"] == "ultra"
    ldr = _water_noise(np.asarray(_canvas_rgb(page)))
    shot(page, "water-ldr-srgb-1440")
    assert errors == []
    ctx.close()
    assert 0.3 < hdr < 3.0, hdr                  # the metric itself is sane
    assert ldr < 4.0 * hdr, (ldr, hdr)


# every framebuffer reports itself unusable from now on: the next rebuild of the
# size-dependent targets (resize, tier change) must fail cleanly, not half-way
BREAK_FBO = "() => { WebGL2RenderingContext.prototype.checkFramebufferStatus = () => 36061; return true; }"


def test_resize_rebuild_failure_demotes_to_2d(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low")
    wait_frames(page, 3, timeout=60_000)
    page.evaluate(BREAK_FBO)
    page.set_viewport_size({"width": 1200, "height": 800})
    wait_for(page, "window.Jellyfield.info().engine === '2d'", 10_000)
    assert page.locator("canvas#jellyfield").count() == 1
    assert [e for e in errors if "CONTEXT_LOST" not in e] == []
    ctx.close()


def test_tier_change_rebuild_failure_demotes_to_2d(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellyslow=40", init_script=STRONG_HOST)
    assert info(page)["tier"] == "ultra"      # there is a step down to fail
    wait_frames(page, 3, timeout=60_000)
    page.evaluate(BREAK_FBO)
    wait_for(page, "window.Jellyfield.info().engine === '2d'", 40_000)   # the step down fails -> 2D
    assert page.locator("canvas#jellyfield").count() == 1
    assert [e for e in errors if "CONTEXT_LOST" not in e] == []
    ctx.close()


# ---- the creature: layout, physics, interaction (Task 5) ----
# heroBox is the bell's CSS-px screen bounds through her live pose and the
# view-projection (updateHeroBox in jellyfield-hd.js): x from a ring of radius
# extent.halfWidth at the bell's origin height (the equator, where the dome is
# widest), the top from the apex at extent.up, the bottom from the 16 margin
# points at wave 0. Tiers are pinned: an unpinned SwiftShader page steps down
# over time, and a tier change rebuilds the meshes mid-test for no reason.


def test_desktop_places_her_beside_the_console(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low")
    wait_frames(page, 10, timeout=60_000)
    hb = info(page)["heroBox"]
    col_right = page.evaluate("document.querySelector('.column').getBoundingClientRect().right")
    assert hb["x"] + hb["w"] * 0.9 > col_right          # at most ~10% behind the glass
    assert hb["x"] + hb["w"] <= 1440 + 2
    assert hb["y"] >= 0
    assert errors == []
    shot(page, "creature-striped-desktop-low")
    ctx.close()


def test_phone_keeps_the_apex_on_screen(server, browser):
    ctx, page, errors = open_page(browser, server, width=390, height=844, dpr=3, query="?jellytier=low")
    wait_frames(page, 10, timeout=60_000)
    hb = info(page)["heroBox"]
    assert hb["y"] >= 0 and hb["x"] >= 0 and hb["x"] + hb["w"] <= 390 + 2
    assert errors == []
    shot(page, "creature-striped-phone-low")
    ctx.close()


def _region_stats(img, x0, x1, y0, y1):
    """(edge fraction, P95 - P5 luminance) of an image region: the share of
    pixels whose luminance step to a neighbour exceeds 6 (dither and grain
    stay under ~2; a silhouette, organ, meridian or strand is far above), and
    how much tonal range the region holds."""
    import numpy as np
    reg = img[y0:y1, x0:x1, :].astype(float)
    lum = 0.2126 * reg[..., 0] + 0.7152 * reg[..., 1] + 0.0722 * reg[..., 2]
    gx = np.abs(np.diff(lum, axis=1))[:-1, :]
    gy = np.abs(np.diff(lum, axis=0))[:, :-1]
    return float(((gx + gy) > 6).mean()), float(np.percentile(lum, 95) - np.percentile(lum, 5))


def test_the_creature_is_actually_drawn(server, browser):
    """Every other HD test would pass with the creature culled: none reads the
    pixels inside heroBox. The engine offers no cull switch, so the control is
    the open water at the same rows on the far side of the console (x 20-180,
    left of .column at 201.6 px): backdrop, shafts and motes are there too,
    and they are smooth and dark. A drawn bell is a large lit body with a
    silhouette, organs and strands, so the box holds edges the water has
    none of and an order of magnitude more tonal range. Measured at Low on
    the striped bell (Task 7): edge fraction 0.11 vs 0.00 in the water, whose
    tonal range is 5-6; the floors are 0.02 and 3x."""
    import numpy as np
    ctx, page, errors = open_page(browser, server, query="?jellytier=low")
    wait_frames(page, 10, timeout=60_000)
    hb = info(page)["heroBox"]
    img = np.asarray(_canvas_rgb(page)).astype(int)
    x0, y0 = int(max(0, hb["x"])), int(max(0, hb["y"]))
    x1, y1 = int(min(1440, hb["x"] + hb["w"])), int(min(900, hb["y"] + hb["h"]))
    assert x1 - x0 > 100 and y1 - y0 > 100
    bell = _region_stats(img, x0, x1, y0, y1)
    water = _region_stats(img, 20, 180, y0, y1)
    assert water[1] > 0                                   # the control is a live frame, not black
    assert bell[0] >= 0.02 and bell[0] > 3 * water[0], (bell, water)
    assert bell[1] > 3 * water[1], (bell, water)
    assert errors == []
    ctx.close()


def test_resize_storm_across_breakpoint(server, browser):
    """The storm steps every 60 ms, under the 150 ms resize debounce, and ends
    where it started, so on its own it may never lay out across the 720 px
    breakpoint. Holding past the debounce at a phone width and then at a
    desktop width runs relayout()'s breakpoint flip both ways (re-anchor and
    chain reset), each checked where she lands and with a frame drawn."""
    ctx, page, errors = open_page(browser, server, query="?jellytier=low&jellydebug=1")
    wait_frames(page, 5, timeout=60_000)
    for w in (1440, 700, 1100, 390, 721, 719, 1440):
        page.set_viewport_size({"width": w, "height": 900})
        page.wait_for_timeout(60)
    page.wait_for_timeout(400)
    wait_frames(page, 10, timeout=60_000)
    assert page.locator("canvas#jellyfield").count() == 1
    assert info(page)["drawW"] == 1440
    assert info(page)["heroBox"] is not None
    # desktop -> phone: centred in the band above the card, apex on screen
    page.set_viewport_size({"width": 390, "height": 900})
    wait_for(page, "window.Jellyfield.info().drawW === 390", 10_000)
    wait_frames(page, 3, timeout=60_000)
    hb = info(page)["heroBox"]
    assert hb["y"] >= 0 and hb["x"] >= 0 and hb["x"] + hb["w"] <= 390 + 2, hb
    # phone -> desktop: beside the console again
    page.set_viewport_size({"width": 1100, "height": 900})
    wait_for(page, "window.Jellyfield.info().drawW === 1100", 10_000)
    wait_frames(page, 3, timeout=60_000)
    hb = info(page)["heroBox"]
    col_right = page.evaluate("document.querySelector('.column').getBoundingClientRect().right")
    assert hb["x"] + hb["w"] * 0.9 > col_right and hb["x"] + hb["w"] <= 1100 + 2, (hb, col_right)
    assert hb["y"] >= 0, hb
    assert page.locator("canvas#jellyfield").count() == 1
    assert [e for e in errors if "GL error" in e] == []
    ctx.close()


def test_pulse_burst_is_bounded(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low&jellydebug=1")
    wait_frames(page, 10, timeout=60_000)
    page.evaluate("for (let i = 0; i < 10; i++) window.Jellyfield.pulse(900, 400, 2.4)")
    wait_frames(page, 30, timeout=60_000)
    assert [e for e in errors if "GL error" in e] == []
    assert errors == []
    ctx.close()
