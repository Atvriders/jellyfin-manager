# Jellyfield HD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the background jellyfish renderer with a WebGL2 HDR multi-pass engine that is sharp at every pixel density, and pick its creature by a two-species bake-off: the moon jelly rebuilt vs a purple-striped jelly.

**Architecture:**
- A new engine file registers `window.JellyfieldHD`.
- The two species live in their own files and plug into a locked contract.
- The existing selector in `jellyfield.js` tries HD first, falls back to the old 3D engine during the bake-off, then to 2D.
- Every frame runs these passes: water → MSAA scene with refraction and interior organ march → god rays → bloom → composite (tonemap, dim band, dither, sharpen).

**Tech Stack:**
- WebGL2 and GLSL ES 3.00; vanilla JS in the same IIFE style as `jellyfield.js`, with no build step.
- Python pytest and Playwright for browser checks.
- Pillow and numpy for the measured judges.

**Spec:** `docs/superpowers/specs/2026-09-29-jellyfield-hd-design.md`. Read it first; this plan argues from it.

## Outcome

The plan ran to the end, and the tasks below are the record of how it got there. What shipped:

- **The owner picked candidate B, the purple-striped jelly** (`jellyfield-species-striped.js`, id `striped`). It is the one creature; the engine has no species switch.
- **Task 10 removed** the moon species (A) and its script tags, and the old 3D engine (`createJellyfield3D`, `selTry3D`, the `classic` param path). The selector order is HD → 2D, and the 2D engine is byte-identical. `?jelly` is gone; `?jellytier`, `?jellyslow`, `?jellydebug` and `?jellyldr` stay for the tests. The capture and judge tools capture the shipped species only.
- **One engine change at the finish, E-S (inextensible chains).** Three Gauss-Seidel passes let B's 192-node arms stretch +21 / +42 / +49 % over 6 s at 16 / 33 / 50 ms frames, so the drape depended on frame rate and node count. A dynamic follow-the-leader pass now caps every link at its rest length. Its velocity term (the parent gives back 0.9 of each correction) is required: without it the arms climbed and coiled over the bell within 2–4 s. B's lengths were re-tuned to the drape the owner judged: the arms from 3.0, which compensated for the stretch, to 3.6 (3.44 deep at every frame rate), and the tentacles from 3.2 to 3.75 (~3.6 deep; the judged frames showed them stretched ×1.14).
- **Not done, by the owner's choice:** the optional engine polish proposals from the rounds (E1, E2, E5, E6, E-A, E-L, E-F).
- **Kept:** the contract surface only A used (the `'fringe'` chain kind, `goldFerrules`).
- The spec's "8 tentacles" for B was wrong: B has 24, in threes, as the animal does.

## Global Constraints

**Commits and interface**
- **One commit at the very end**, after the owner's pick and the full verification. Never commit per task (standing owner rule).
- `window.Jellyfield` keeps `{ mount, pulse, setActivity }` exactly. It gains only a read-only `info()`.
- Templates change only by adding `<script>` tags. The scan console, Incoming panel and login page code are untouched.

**GL requirements**
- WebGL2 is required. Context attributes: `{alpha:false, antialias:false, depth:false, stencil:false, premultipliedAlpha:false, powerPreference:'high-performance'}`.
- HDR targets are `RGBA16F` when `EXT_color_buffer_float` exists. Otherwise LDR mode: `SRGB8_ALPHA8` plus a 0.25 pre-exposure.
- Drawing buffer is `CSS × min(devicePixelRatio, 3)`, capped at 8,294,400 px total.

**Tiers**

| Tier | Render scale | MSAA | Bloom levels | God-ray samples | Organ steps | Bell mesh |
|---|---|---|---|---|---|---|
| Ultra | 1.0 | 4 | 6 | 64 | 12 | 256×128 |
| High | 0.85 | 2 | 5 | 40 | 8 | 192×96 |
| Low | 0.7 | 0 (FXAA) | 4 | 24 | 5 | 128×64 |

- **Starting tier:** High when `W ≤ 720`, or `deviceMemory ≤ 4`, or `hardwareConcurrency ≤ 4`; otherwise Ultra.
- **Stepping:** step down when EMA > 20 ms for 2 s; step up when EMA < 11 ms for 10 s. At most one change per 8 s, and never above the starting tier's ceiling on phones.

**Code discipline**
- **Zero per-frame allocation.** All typed arrays are preallocated at mount or at tier change.
- **Bounded phases:** every time fed to a shader is `timeS % period`, with rates quantized to whole cycles per period.
- **No `sin`-based hash.** Reuse the existing no-sin `vhash` (`HASH_GLSL` in `jellyfield.js`), ported to GLSL 3.00.
- **Context loss:** every GL object is rebuilt on restore — programs, buffers, VAOs, textures, framebuffers and renderbuffers. A context still lost after 3 s of visible time calls `onFatal` and demotes to 2D.
- **Carried over from today:** pause while `document.hidden`; debounced resize at 150 ms; mouse parallax ±3°; the `pulse()` and `setActivity()` semantics and numbers.

**Aesthetics and hygiene**
- Owner aesthetic rules:
  - creature first; divinity only through light and restrained gold;
  - no wings, halo, crown or gemstones;
  - white-gold only in light (shafts, glow, motes, apex star);
  - the apex star never clips to white.
- No real LAN addresses or hostnames in any committed file.

## Review Focus

These are the inputs most likely to bite a real visitor that no feature test naturally hits. Each is pinned by a test in the named task.
1. **A lost context that comes back while the tab is hidden.** It must rebuild on the next visible frame, not render into dead objects. Pinned by `test_context_restored_while_hidden_rebuilds_on_return` (Task 2).
2. **A resize storm across the phone/desktop breakpoint (720 px) during rendering.** The creature must re-layout with no GL errors, the canvas count must stay 1, and there must be no leaked targets. Pinned by `test_resize_storm_across_breakpoint` (Task 5).
3. **A 4K monitor at 2× DPR.** The drawing buffer must respect the 8.3 MP cap, not allocate 33 MP. Pinned by `test_4k_at_2x_respects_pixel_cap` (Task 2).
4. **A GPU without `EXT_color_buffer_float`.** LDR mode must still render: no black frame, no errors. Pinned by `test_ldr_mode_renders` (Task 3), forced with `?jellyldr=1`.
5. **A burst of scan presses (`pulse()` ×10 in 1 s).** The shell pool must stay capped at 6, with no allocation growth and no GL errors. Pinned by `test_pulse_burst_is_bounded` (Task 5).

## About the visual tasks (6–7)

A creature's look is not a predictable function of code written in advance. It is refined against rendered frames. So Tasks 6 and 7 are specified by:
- the **locked contract** (exact field names and GLSL signatures);
- **exact starting code** for the shape (candidate A reuses today's `bellPt` math);
- **measurable acceptance criteria**, checked by the judge tool from Task 8.

Everything else in this plan has exact code.

---

### Task 1: Scaffolding — registry, selector, script tags, `info()`, bake-off params

**Files:**
- Create: `app/static/jellyfield-hd.js`
- Create: `app/static/jellyfield-species-moon.js`, `app/static/jellyfield-species-striped.js` (registration stubs)
- Modify: `app/static/jellyfield.js` (selector section only)
- Modify: `app/templates/index.html`, `app/templates/login.html` (script tags only)
- Test: `app/tests/test_jellyfield_hd_e2e.py` (new), `app/tests/test_template.py` (append)

**Interfaces:**
- Produces:
  - `window.JellyfieldHD = { create() -> engine, species: {}, params: {jelly, tier, slow, debug, ldr} }`
  - engine = `{ mount(canvas) -> Boolean, pulse(x, y, s), setActivity(l), detach(), info() -> Object, onFatal }`
  - `window.Jellyfield.info() -> { engine: 'hd'|'3d'|'2d'|'', species, tier, dpr, drawW, drawH, renderScale, hdr, frames, heroBox }` (fields `null` when the active engine doesn't know them)

- [ ] **Step 1: Write the failing tests.** Create `app/tests/test_jellyfield_hd_e2e.py`:

```python
"""Browser checks for the Jellyfield HD engine (real Chrome via playwright).

Skipped without playwright or a browser (CI). Uses SwiftShader for WebGL.
Set JELLY_SHOTS=/dir to save frames for a human to look at.
"""

import os
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

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


def open_page(browser, url, width=1440, height=900, dpr=1, reduced=False, query=""):
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=dpr,
                              reduced_motion="reduce" if reduced else "no-preference")
    page = ctx.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url + query)
    page.wait_for_function("window.Jellyfield && window.Jellyfield.info && window.Jellyfield.info().engine !== ''")
    return ctx, page, errors


def info(page):
    return page.evaluate("window.Jellyfield.info()")


def wait_frames(page, n=20, timeout=20_000):
    start = info(page)["frames"] or 0
    page.wait_for_function(f"(window.Jellyfield.info().frames || 0) >= {start + n}", timeout=timeout)


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
    assert i["species"] in ("moon", "striped")
    assert errors == []
    shot(page, "hd-default-1440")
    ctx.close()


def test_species_and_classic_params(server, browser):
    for q, engine, species in (("?jelly=moon", "hd", "moon"), ("?jelly=striped", "hd", "striped"),
                               ("?jelly=classic", "3d", None)):
        ctx, page, errors = open_page(browser, server, query=q)
        assert info(page)["engine"] == engine
        if species:
            assert info(page)["species"] == species
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
    page.wait_for_function("window.Jellyfield.info().engine === 'hd'", timeout=10_000)
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function("window.Jellyfield.info().engine === '2d'", timeout=10_000)
    assert page.locator("canvas#jellyfield").count() == 1
    ctx.close()
```

Append to `app/tests/test_template.py`:

```python
def test_hd_engine_scripts_load_before_the_selector():
    for path in (TEMPLATE, LOGIN):
        text = path.read_text(encoding="utf-8")
        hd = text.index("filename='jellyfield-hd.js'")
        moon = text.index("filename='jellyfield-species-moon.js'")
        striped = text.index("filename='jellyfield-species-striped.js'")
        sel = text.index("filename='jellyfield.js'")
        assert hd < moon < sel and hd < striped < sel
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py app/tests/test_template.py -q`
Expected: FAIL. `info` is not a function, `jellyfield-hd.js` is not in the templates, and `engine` never becomes `'hd'`.

- [ ] **Step 3: Implement.**

(a) Create `app/static/jellyfield-hd.js`. This is the skeleton; later tasks fill in `createJellyfieldHD`. For now, `mount` returns `false` unless WebGL2 exists, and when it exists it clears to the abyss colour every frame. That is enough to make selection testable.

```js
/* Jellyfield HD — WebGL2 HDR multi-pass jellyfish.
 * Spec: docs/superpowers/specs/2026-09-29-jellyfield-hd-design.md
 * Registers window.JellyfieldHD; the selector in jellyfield.js owns window.Jellyfield. */
(function () {
  'use strict';

  var SPECIES = {};

  /* bake-off / test knobs, read once */
  var PARAMS = (function () {
    var q = {};
    try {
      var sp = new URLSearchParams(window.location.search);
      q.jelly = sp.get('jelly');
      q.tier = sp.get('jellytier');
      q.slow = sp.has('jellyslow') ? Number(sp.get('jellyslow')) : null;
      q.debug = sp.get('jellydebug') === '1';
      q.ldr = sp.get('jellyldr') === '1';
    } catch (_) { /* old browsers: defaults */ }
    return q;
  })();

  function pickSpecies() {
    if (PARAMS.jelly && SPECIES[PARAMS.jelly]) { return SPECIES[PARAMS.jelly]; }
    return SPECIES.moon || SPECIES.striped || null;
  }

  function createJellyfieldHD() {
    var canvas = null, gl = null, mounted = false, rafId = 0, frames = 0, running = false;
    var species = null;
    var api = { mount: mount, pulse: pulse, setActivity: setActivity, detach: detach, info: info, onFatal: null };

    function frame() {
      rafId = 0;
      if (!running) { return; }
      frames++;
      gl.clearColor(0.016, 0.039, 0.071, 1);
      gl.clear(gl.COLOR_BUFFER_BIT);
      rafId = requestAnimationFrame(frame);
    }

    function mount(canvasEl) {
      if (mounted) { return true; }
      species = pickSpecies();
      if (!species || !canvasEl) { return false; }
      try {
        gl = canvasEl.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false,
          premultipliedAlpha: false, powerPreference: 'high-performance' });
      } catch (_) { gl = null; }
      if (!gl) { return false; }
      canvas = canvasEl;
      mounted = true;
      running = true;
      rafId = requestAnimationFrame(frame);
      return true;
    }

    function pulse() {}
    function setActivity() {}

    function detach() {
      running = false;
      if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
      mounted = false;
      gl = null;
      canvas = null;
    }

    function info() {
      return { species: species ? species.id : null, tier: null, dpr: null, drawW: canvas ? canvas.width : null,
               drawH: canvas ? canvas.height : null, renderScale: null, hdr: null, frames: frames, heroBox: null };
    }

    return api;
  }

  window.JellyfieldHD = { create: createJellyfieldHD, species: SPECIES, params: PARAMS };
})();
```

(b) Create the two species stubs. They are filled in Tasks 6 and 7. `app/static/jellyfield-species-moon.js`:

```js
/* Candidate A — the moon jelly, rebuilt. Contract: spec "The species contract". */
(function () {
  'use strict';
  if (!window.JellyfieldHD) { return; }
  window.JellyfieldHD.species.moon = {
    id: 'moon',
    extent: { halfWidth: 0.94, up: 0.74, down: 0.70 },
    breath: 4.5,
    palette: { body: [0.42, 0.40, 0.95], rim: [0.75, 0.85, 1.4], gold: [1.6, 1.15, 0.55],
               glow: [1.4, 1.2, 0.8], organ: [1.1, 0.55, 0.9], filament: [0.8, 0.8, 1.2] },
    bell: { glsl: '' },
    chains: [],
    motion: { contraction: 0.22, rimFlutter: 0.015, sway: 1 }
  };
})();
```

`app/static/jellyfield-species-striped.js` has the same shape with `id: 'striped'`, `extent: { halfWidth: 0.9, up: 0.9, down: 3.6 }`, and `palette: { body: [0.78, 0.74, 0.95], rim: [0.9, 0.9, 1.3], gold: [1.4, 1.05, 0.5], glow: [1.3, 1.15, 0.85], organ: [0.9, 0.5, 1.1], filament: [0.35, 0.12, 0.45] }`.

(c) Selector, in `app/static/jellyfield.js`'s selector section:

1. Add `var selHD = null;` beside `sel3d`.
2. Add the three functions below.
3. Change `selMount` to try `selTryHD()`, then `selTry3D()` (with `selFreshCanvas()` between the attempts), then `selUse2D()`.
4. In `selOnMotionChange`, treat `selKind === 'hd'` exactly like `'3d'`: demote to 2D on reduce. On promotion, try HD first, then 3D.
5. Export `info: selInfo`.

```js
  function selParamClassic() {
    return !!(window.JellyfieldHD && window.JellyfieldHD.params && window.JellyfieldHD.params.jelly === 'classic');
  }

  function selTryHD() {
    var HD = window.JellyfieldHD;
    if (!HD || selParamClassic()) { return false; }
    if (!selHD) { selHD = HD.create(); selHD.onFatal = selOnFatalHD; }
    if (selHD.mount(selCanvas)) {
      selActive = selHD;
      selKind = 'hd';
      selHD.setActivity(selLastActivity);
      return true;
    }
    return false;
  }

  function selOnFatalHD() {
    /* the GPU context died for good: 2D for the session (not the old 3D engine,
       whose context would die the same way) */
    if (selKind !== 'hd') { return; }
    selHD.detach();
    selFreshCanvas();
    selUse2D();
  }

  function selInfo() {
    var base = (selActive && typeof selActive.info === 'function') ? selActive.info() : {};
    var out = { engine: selKind, species: null, tier: null, dpr: null, drawW: null, drawH: null,
                renderScale: null, hdr: null, frames: null, heroBox: null };
    var k;
    for (k in base) { if (Object.prototype.hasOwnProperty.call(base, k)) { out[k] = base[k]; } }
    out.engine = selKind;
    return out;
  }
```

`window.Jellyfield = { mount: selMount, pulse: selPulse, setActivity: selSetActivity, info: selInfo };`

The 2D and 3D engines get no `info()`, so `selInfo` reports `engine` and nulls for them. The frame count needed by tests only matters for HD.

(d) Templates. In both `index.html` and `login.html`, directly **before** the existing `jellyfield.js` tag, add:

```html
<script src="{{ url_for('static', filename='jellyfield-hd.js') }}"></script>
<script src="{{ url_for('static', filename='jellyfield-species-moon.js') }}"></script>
<script src="{{ url_for('static', filename='jellyfield-species-striped.js') }}"></script>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py app/tests/test_template.py -q`
Expected: PASS. The HD skeleton mounts and clears to the abyss colour, so the page is temporarily jellyfish-free in HD. That's acceptable mid-plan, because nothing is committed until the end.

Then run the full suite. Expected: everything else is still green.

---

### Task 2: GL foundation — capabilities, resources, sizing, lifecycle

**Files:**
- Modify: `app/static/jellyfield-hd.js` (inside `createJellyfieldHD`)
- Test: `app/tests/test_jellyfield_hd_e2e.py` (append)

**Interfaces:**
- Consumes: Task 1's skeleton.
- Produces, inside the engine closure (used by Tasks 3–5):
  - `caps = { hdr: Boolean, maxSamples: Number, maxTex: Number }`
  - `res` is a registry. `res.program(vsSrc, fsSrc, attribs) -> {p, u: {name: loc}}`, `res.buffer()`, `res.vao()`, `res.texture(w, h, hdr) -> tex`, `res.fbo(tex) -> fbo`, `res.msaaFbo(w, h, samples, hdr) -> {fbo, rb}`, `res.releaseAll()`. Every created object is recorded; `releaseAll()` deletes each one.
  - `sizeTargets()` re-creates every size-dependent target from `drawW`, `drawH` and the tier's render scale.
  - Lifecycle: `start()`, `stop()`, `onCtxLost(e)`, `onCtxRestored()`, `armLostTimer()` (3 s of visible time → `api.onFatal()`), `onVisibility()`, `onResize()` (150 ms debounce).
  - `drawW = round(cssW × dprEff)`, `drawH = round(cssH × dprEff)`, where `dprEff = min(devicePixelRatio, 3)`, reduced so that `drawW × drawH ≤ 8294400`.

- [ ] **Step 1: Write the failing tests.** Append:

```python
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
    page.wait_for_function("window.Jellyfield.info().engine === '2d'", timeout=8_000)
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


def test_heap_is_stable_over_a_minute(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low")
    wait_frames(page, 30, timeout=60_000)
    page.wait_for_timeout(3000)
    h0 = page.evaluate("performance.memory.usedJSHeapSize")
    page.wait_for_timeout(60_000)
    h1 = page.evaluate("performance.memory.usedJSHeapSize")
    assert h1 - h0 < 2_000_000
    ctx.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q -k "drawing_buffer or pixel_cap or hidden or context or heap"`
Expected: FAIL. `dpr` is null, the buffer isn't sized, and there's no pause, loss handling or demotion yet.

- [ ] **Step 3: Implement** the foundation inside `createJellyfieldHD`. The required code shapes are below; the prose after them lists the remaining rules.

```js
    var PIXEL_CAP = 8294400;
    var caps = { hdr: false, maxSamples: 0, maxTex: 0 };
    var cssW = 0, cssH = 0, dprEff = 1, drawW = 0, drawH = 0;
    var contextLost = false, lostTimer = 0, lostVisibleMs = 0, LOST_GRACE_MS = 3000, dead = false;

    function detectCaps() {
      caps.hdr = !window.JellyfieldHD.params.ldr && !!gl.getExtension('EXT_color_buffer_float');
      gl.getExtension('OES_texture_float_linear');   /* optional: smoother HDR sampling */
      caps.maxSamples = gl.getParameter(gl.MAX_SAMPLES) || 0;
      caps.maxTex = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 4096;
    }

    function computeSize() {
      var rect = canvas.getBoundingClientRect();
      cssW = rect.width || window.innerWidth;
      cssH = rect.height || window.innerHeight;
      dprEff = Math.min(3, window.devicePixelRatio || 1);
      var w = cssW * dprEff, h = cssH * dprEff;
      if (w * h > PIXEL_CAP) { var k = Math.sqrt(PIXEL_CAP / (w * h)); w *= k; h *= k; }
      drawW = Math.max(1, Math.min(caps.maxTex, Math.round(w)));
      drawH = Math.max(1, Math.min(caps.maxTex, Math.round(h)));
      canvas.width = drawW; canvas.height = drawH;
    }

    var res = (function () {
      var owned = [];
      function track(kind, obj) { owned.push([kind, obj]); return obj; }
      return {
        program: function (vs, fs, attribs) {
          function sh(type, src) {
            var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
            if (!gl.getShaderParameter(s, gl.COMPILE_STATUS) && !gl.isContextLost()) {
              throw new Error('shader: ' + gl.getShaderInfoLog(s));
            }
            return s;
          }
          var p = gl.createProgram();
          var v = sh(gl.VERTEX_SHADER, vs), f = sh(gl.FRAGMENT_SHADER, fs);
          gl.attachShader(p, v); gl.attachShader(p, f);
          var name;
          for (name in attribs) { if (Object.prototype.hasOwnProperty.call(attribs, name)) { gl.bindAttribLocation(p, attribs[name], name); } }
          gl.linkProgram(p);
          gl.deleteShader(v); gl.deleteShader(f);
          if (!gl.getProgramParameter(p, gl.LINK_STATUS) && !gl.isContextLost()) {
            throw new Error('link: ' + gl.getProgramInfoLog(p));
          }
          var u = {}, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS), i;
          for (i = 0; i < n; i++) {
            var a = gl.getActiveUniform(p, i);
            var key = a.name.replace(/\[0\]$/, '');
            u[key] = gl.getUniformLocation(p, a.name);
          }
          track('program', p);
          return { p: p, u: u };
        },
        buffer: function () { return track('buffer', gl.createBuffer()); },
        vao: function () { return track('vao', gl.createVertexArray()); },
        texture: function (w, h, hdr) {
          var t = gl.createTexture();
          gl.bindTexture(gl.TEXTURE_2D, t);
          gl.texImage2D(gl.TEXTURE_2D, 0, hdr ? gl.RGBA16F : gl.RGBA8, w, h, 0, gl.RGBA,
                        hdr ? gl.HALF_FLOAT : gl.UNSIGNED_BYTE, null);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
          return track('texture', t);
        },
        fbo: function (tex) {
          var f = gl.createFramebuffer();
          gl.bindFramebuffer(gl.FRAMEBUFFER, f);
          gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
          return track('fbo', f);
        },
        msaaFbo: function (w, h, samples, hdr) {
          var rb = track('rb', gl.createRenderbuffer());
          gl.bindRenderbuffer(gl.RENDERBUFFER, rb);
          gl.renderbufferStorageMultisample(gl.RENDERBUFFER, samples, hdr ? gl.RGBA16F : gl.RGBA8, w, h);
          var f = track('fbo', gl.createFramebuffer());
          gl.bindFramebuffer(gl.FRAMEBUFFER, f);
          gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, rb);
          return { fbo: f, rb: rb };
        },
        /* size-dependent objects are released by kind-tag when resizing */
        releaseAll: function () {
          if (gl && !gl.isContextLost()) {
            owned.forEach(function (o) {
              var k = o[0], x = o[1];
              if (k === 'program') { gl.deleteProgram(x); }
              else if (k === 'buffer') { gl.deleteBuffer(x); }
              else if (k === 'vao') { gl.deleteVertexArray(x); }
              else if (k === 'texture') { gl.deleteTexture(x); }
              else if (k === 'fbo') { gl.deleteFramebuffer(x); }
              else if (k === 'rb') { gl.deleteRenderbuffer(x); }
            });
          }
          owned.length = 0;   /* after a loss nothing belongs to the live context */
        }
      };
    })();
```

**Lifecycle rules**
- `mount` calls `detectCaps()`, then `computeSize()`, then `buildAll()`. Task 3 fills in `buildAll`; for now it's a no-op.
- It adds these listeners: `webglcontextlost` (`preventDefault` plus `onCtxLost`), `webglcontextrestored`, window `resize` (150 ms debounce into `computeSize()` + `sizeTargets()`), and document `visibilitychange`.
- **`onCtxLost`:** stop the loop, forget the resource registry with `owned.length = 0` (no GL calls), and arm the lost timer.
- **Lost timer:** it only counts time while `!document.hidden`, polling every 250 ms. After 3000 ms it sets `dead = true` and calls `api.onFatal()`.
- **`onCtxRestored`:** if `dead`, return. Otherwise clear the timer, `detectCaps()`, `computeSize()`, `buildAll()`. Then `start()` only if the document is visible; `onVisibility` calls `start()` later.
- **`onVisibility`:** hidden → `stop()`; visible and not lost → `start()`.
- **`detach()`:** remove every listener, `res.releaseAll()`, `WEBGL_lose_context.loseContext()` if the extension is available, and null everything.

`info()` now returns:
- `dpr: dprEff`, `drawW`, `drawH`, `hdr: caps.hdr`, `frames`;
- `tier` and `renderScale` (Task 4 fills these in);
- `heroBox` (Task 5 fills this in).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q -k "drawing_buffer or pixel_cap or hidden or context or heap"`
Expected: PASS.

---

### Task 3: The pass pipeline — water, scene target, god rays, bloom, composite

**Files:**
- Modify: `app/static/jellyfield-hd.js`
- Test: `app/tests/test_jellyfield_hd_e2e.py` (append); `tools/jelly_judge.py` (Task 8) is used by the banding and clipping tests

**Interfaces:**
- Consumes: Task 2's `res`, `caps`, `drawW`/`drawH` and lifecycle.
- Produces:
  - `buildAll()` compiles every program and builds static buffers and VAOs.
  - `sizeTargets()` builds `waterTex` (½ scale), `scene` (MSAA FBO at render scale), `sceneTex` (the resolve target), `rayTex` (¼ scale), `bloomTex[0..L-1]` (halving chain) and their FBOs.
  - `renderFrame(dt)` runs water → scene → rays → bloom → composite.
  - `drawCreature()` is a hook, left empty until Task 5.
  - The scene alpha channel is creature coverage.

- [ ] **Step 1: Write the failing tests.** Append:

```python
def _canvas_rgb(page):
    import io
    from PIL import Image
    png = page.locator("canvas#jellyfield").screenshot()
    return Image.open(io.BytesIO(png)).convert("RGB")


def test_water_renders_a_banding_free_gradient(server, browser):
    import numpy as np
    ctx, page, errors = open_page(browser, server, query="?jellytier=ultra")
    wait_frames(page, 20, timeout=60_000)
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
    img = np.asarray(_canvas_rgb(page))
    assert img.mean() > 8
    assert errors == []
    ctx.close()


def test_debug_mode_reports_no_gl_errors(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellydebug=1")
    wait_frames(page, 30, timeout=60_000)
    assert [e for e in errors if "GL error" in e] == []
    ctx.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q -k "water or ldr or debug"`
Expected: FAIL. The frame is a flat clear colour: a giant flat run, or a black frame.

- [ ] **Step 3: Implement the passes.** All shaders start with `#version 300 es` and `precision highp float;` (ES 3.0 guarantees highp in fragment shaders).

**Full-screen triangle, shared by every pass.** Drawn from `gl_VertexID` with no buffer:

```glsl
#version 300 es
out vec2 v_uv;
void main() {
  vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
  v_uv = p;
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
```

**Water pass (½ scale).** Port the old engine's backdrop, silhouette, mote and shell shaders from `jellyfield.js`: `BACK_VS`/`BACK_FS`, `SIL_*`, `MOTE_*` and `SHELL_*`.
- Change `attribute`/`varying` to `in`/`out`, and `gl_FragColor` to an `out vec4`.
- Remove the in-shader tonemaps: this pass outputs linear HDR.
- Keep every bounded-phase and `vhash` rule.
- Keep the crepuscular shafts converging on the hero anchor.
- Output alpha is 0. Coverage is written only by the creature.

**God rays (¼ scale):**

```glsl
#version 300 es
precision highp float;
in vec2 v_uv; out vec4 o;
uniform sampler2D u_scene;      // sceneTex
uniform vec2 u_light;           // surface-light position in uv (above the hero, y ~ 1.05)
uniform int u_samples;          // 64 / 40 / 24 by tier
uniform float u_decay, u_density, u_weight;
void main() {
  vec2 d = (v_uv - u_light) * (u_density / float(u_samples));
  vec2 uv = v_uv; float illum = 1.0; vec3 acc = vec3(0.0);
  for (int i = 0; i < 64; i++) {
    if (i >= u_samples) break;
    uv -= d;
    vec4 s = texture(u_scene, uv);
    float lum = max(dot(s.rgb, vec3(0.2126, 0.7152, 0.0722)) - 0.6, 0.0);  // light only
    acc += s.rgb * lum * (1.0 - s.a) * illum * u_weight;                    // the bell occludes
    illum *= u_decay;
  }
  o = vec4(acc, 1.0);
}
```

**Bloom: dual-filter Kawase.** The down pass samples 4 diagonal taps plus the centre. The first level thresholds with a soft knee: `max(c - 1.0, 0) + knee`. The up pass is a 9-tap tent, adding into the previous level with additive blending.

```glsl
// down
uniform sampler2D u_src; uniform vec2 u_texel; uniform float u_first;
void main() {
  vec3 c = texture(u_src, v_uv).rgb * 4.0;
  c += texture(u_src, v_uv + vec2(-1.0, -1.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2( 1.0, -1.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2(-1.0,  1.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2( 1.0,  1.0) * u_texel).rgb;
  c /= 8.0;
  if (u_first > 0.5) { float l = max(c.r, max(c.g, c.b)); c *= smoothstep(0.8, 1.6, l); }
  o = vec4(c, 1.0);
}
// up
uniform sampler2D u_src; uniform vec2 u_texel;
void main() {
  vec3 c = texture(u_src, v_uv + vec2(-2.0, 0.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2(-1.0, 1.0) * u_texel).rgb * 2.0;
  c += texture(u_src, v_uv + vec2(0.0, 2.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2(1.0, 1.0) * u_texel).rgb * 2.0;
  c += texture(u_src, v_uv + vec2(2.0, 0.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2(1.0, -1.0) * u_texel).rgb * 2.0;
  c += texture(u_src, v_uv + vec2(0.0, -2.0) * u_texel).rgb;
  c += texture(u_src, v_uv + vec2(-1.0, -1.0) * u_texel).rgb * 2.0;
  o = vec4(c / 12.0, 1.0);
}
```

**Composite** to the default framebuffer at `drawW × drawH`:

```glsl
#version 300 es
precision highp float;
in vec2 v_uv; out vec4 o;
uniform sampler2D u_scene, u_bloom, u_rays;
uniform vec2 u_texel;          // 1 / drawSize
uniform float u_exposure;      // 1.0 HDR, 4.0 in LDR mode (undo the 0.25 pre-exposure)
uniform float u_bloomAmt, u_rayAmt;
uniform vec3 u_dim;            // dimL, dimR (device px) and feather; dimR < 0 disables
uniform float u_dimFloor;
uniform float u_fxaa;          // 1 on Low
uniform float u_frame;         // bounded 0..255 for the dither
// vhash: paste the GLSL 3.00 port of HASH_GLSL from jellyfield.js here, with the
// same math verbatim (the no-sin, mediump-safe hash). Only the `varying`/`attribute`
// keywords change; the function body does not.
vec3 agx(vec3 c) {             // AgX-style: log2 encode, sigmoid, preserves hue into the highlights
  c = max(c, 0.0);
  vec3 l = clamp((log2(c + 1e-5) + 12.47393) / 16.5, 0.0, 1.0);
  vec3 l2 = l * l, l4 = l2 * l2;
  return 15.5 * l4 * l2 - 40.14 * l4 * l + 31.96 * l4 - 6.868 * l2 * l + 0.4298 * l2 + 0.1191 * l - 0.00232;
}
vec3 sceneAt(vec2 uv) { return texture(u_scene, uv).rgb; }
void main() {
  vec3 c = sceneAt(v_uv);
  if (u_fxaa > 0.5) {          // cheap luma-edge blend on Low (no MSAA there)
    vec3 n = sceneAt(v_uv + vec2(0.0, u_texel.y)), s = sceneAt(v_uv - vec2(0.0, u_texel.y));
    vec3 e = sceneAt(v_uv + vec2(u_texel.x, 0.0)), w = sceneAt(v_uv - vec2(u_texel.x, 0.0));
    float edge = length(n - s) + length(e - w);
    c = mix(c, (n + s + e + w + c) * 0.2, clamp(edge * 2.0, 0.0, 0.6));
  }
  // contrast-adaptive sharpen (CAS-lite)
  vec3 blur = (sceneAt(v_uv + vec2(u_texel.x, 0.0)) + sceneAt(v_uv - vec2(u_texel.x, 0.0)) +
               sceneAt(v_uv + vec2(0.0, u_texel.y)) + sceneAt(v_uv - vec2(0.0, u_texel.y))) * 0.25;
  c += (c - blur) * 0.35;
  c = c * u_exposure + texture(u_bloom, v_uv).rgb * u_bloomAmt + texture(u_rays, v_uv).rgb * u_rayAmt;
  vec3 col = agx(c);
  float px = v_uv.x / u_texel.x;
  if (u_dim.y > 0.0) {           // the a11y dim band behind the text column (same rule as today)
    float inBand = smoothstep(u_dim.x - u_dim.z, u_dim.x, px) * (1.0 - smoothstep(u_dim.y, u_dim.y + u_dim.z, px));
    col *= mix(1.0, max(0.42, u_dimFloor), inBand);
  }
  vec2 q = v_uv - 0.5; col *= 1.0 - 0.28 * dot(q, q);                         // vignette
  col += (vhash(gl_FragCoord.xy + u_frame * 1.618) - 0.5) / 255.0;            // dither
  o = vec4(clamp(col, 0.0, 1.0), 1.0);
}
```

**`renderFrame(dt)` order:**
1. Bind the `waterTex` FBO at ½ size and draw water.
2. Bind the scene MSAA FBO at render size, clear to `(0,0,0,0)`, blit `waterTex` with the full-screen program (alpha 0), then call `drawCreature()`.
3. `blitFramebuffer` MSAA → the `sceneTex` FBO. On Low (no MSAA), render straight into the `sceneTex` FBO.
4. God rays → `rayTex`.
5. Bloom down/up chain.
6. Composite to the default framebuffer, with `gl.viewport(0, 0, drawW, drawH)`.

When `params.debug` is set, check `gl.getError()` after each pass and `console.error('GL error ' + err + ' after ' + passName)`.

Uniform arrays and all per-frame values go through preallocated `Float32Array`s.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q`
Expected: PASS. **Look at** `JELLY_SHOTS` frames of the water alone: shafts converge on the hero anchor, motes rise, and there's no banding.

---

### Task 4: Quality tiers and adaptive stepping

**Files:**
- Modify: `app/static/jellyfield-hd.js`
- Test: `app/tests/test_jellyfield_hd_e2e.py` (append)

**Interfaces:**
- Produces:
  - `TIERS = { ultra: {...}, high: {...}, low: {...} }`, with the values from Global Constraints plus `chainScale` (1, 1, 0.6) and `fringeScale` (1, 0.75, 0.4).
  - `tier` (a string).
  - `setTier(name)` re-runs `sizeTargets()` and rebuilds the tier-dependent meshes.
  - `tierTick(frameMs)` is called each frame.
  - `info().tier` and `info().renderScale`.

- [ ] **Step 1: Write the failing tests.**

```python
def test_tier_can_be_pinned(server, browser):
    for t in ("ultra", "high", "low"):
        ctx, page, errors = open_page(browser, server, query=f"?jellytier={t}")
        wait_frames(page, 3, timeout=60_000)
        assert info(page)["tier"] == t
        ctx.close()


def test_phone_starts_at_high(server, browser):
    ctx, page, errors = open_page(browser, server, width=390, height=844, dpr=3, query="?jellyslow=8")
    wait_frames(page, 3, timeout=60_000)
    assert info(page)["tier"] == "high"
    ctx.close()


def test_slow_frames_step_down_then_recover(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellyslow=40")
    start = info(page)["tier"]
    assert start == "ultra"
    page.wait_for_function("window.Jellyfield.info().tier !== 'ultra'", timeout=6_000)
    page.evaluate("window.__jellyfieldSlow = 5")
    page.wait_for_function("window.Jellyfield.info().tier === 'ultra'", timeout=40_000)
    ctx.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q -k tier`
Expected: FAIL. `tier` is null.

- [ ] **Step 3: Implement.**

```js
    var TIERS = {
      ultra: { scale: 1.0, msaa: 4, bloom: 6, rays: 64, organ: 12, bellSeg: 256, bellRings: 128, chainScale: 1, fringeScale: 1 },
      high:  { scale: 0.85, msaa: 2, bloom: 5, rays: 40, organ: 8, bellSeg: 192, bellRings: 96, chainScale: 1, fringeScale: 0.75 },
      low:   { scale: 0.7, msaa: 0, bloom: 4, rays: 24, organ: 5, bellSeg: 128, bellRings: 64, chainScale: 0.6, fringeScale: 0.4 }
    };
    var ORDER = ['low', 'high', 'ultra'];
    var tier = 'ultra', ceiling = 'ultra', ema = 16, slowFor = 0, fastFor = 0, sinceChange = 1e9;

    function startingTier() {
      var P = window.JellyfieldHD.params;
      if (P.tier && TIERS[P.tier]) { ceiling = P.tier; return P.tier; }
      var weak = cssW <= 720 || (navigator.deviceMemory && navigator.deviceMemory <= 4) ||
                 (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4);
      ceiling = weak ? 'high' : 'ultra';
      return ceiling;
    }

    function tierTick(frameMs, dt) {
      var P = window.JellyfieldHD.params;
      if (P.tier) { return; }                                  /* pinned */
      if (P.slow !== null) { frameMs = (typeof window.__jellyfieldSlow === 'number') ? window.__jellyfieldSlow : P.slow; }
      ema += (frameMs - ema) * 0.08;
      sinceChange += dt;
      if (ema > 20) { slowFor += dt; fastFor = 0; } else if (ema < 11) { fastFor += dt; slowFor = 0; } else { slowFor = 0; fastFor = 0; }
      if (sinceChange < 8) { return; }
      var i = ORDER.indexOf(tier);
      if (slowFor > 2 && i > 0) { setTier(ORDER[i - 1]); }
      else if (fastFor > 10 && i < ORDER.indexOf(ceiling)) { setTier(ORDER[i + 1]); }
    }

    function setTier(name) {
      if (name === tier && sinceChange < 1e8) { return; }
      tier = name; sinceChange = 0; slowFor = 0; fastFor = 0;
      sizeTargets();
      rebuildTierMeshes();      /* Task 5: bell grid + chain/fringe counts */
    }
```

`frameMs` is the wall time between RAF callbacks, clamped to 250 ms. The first 30 frames after a tier change are ignored, so shader warm-up doesn't count.

Add `function rebuildTierMeshes() {}` as a no-op in this task; Task 5 replaces its body.

The recovery test needs about 10 s of fast frames, plus up to 8 s since the last change. Its 40 s timeout covers that.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q -k tier`
Expected: PASS.

---

### Task 5: Shared creature systems — layout, physics, interaction, bell/filament/frill renderers

**Files:**
- Modify: `app/static/jellyfield-hd.js`
- Test: `app/tests/test_jellyfield_hd_e2e.py` (append)

**Interfaces:**
- Consumes: the species object (contract in the spec). `species.bell.glsl` must define `bellShape`, `bellThickness`, `bellSurface` and `organField` with the exact signatures in the spec.
- Produces:
  - `setupHero()`: layout driven by `species.extent`.
  - `drawCreature()`: bell back faces, bell front faces, then filaments, frills and fringe.
  - `pulse()` and `setActivity()` with today's semantics and numbers.
  - `info().heroBox = { x, y, w, h }`: the bell's CSS-px screen bounds, from projecting the `bellShape` margin ring and apex.

- [ ] **Step 1: Write the failing tests.**

```python
def test_desktop_places_her_beside_the_console(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low")
    wait_frames(page, 10, timeout=60_000)
    hb = info(page)["heroBox"]
    col_right = page.evaluate("document.querySelector('.column').getBoundingClientRect().right")
    assert hb["x"] + hb["w"] * 0.9 > col_right          # at most ~10% behind the glass
    assert hb["x"] + hb["w"] <= 1440 + 2
    assert hb["y"] >= 0
    ctx.close()


def test_phone_keeps_the_apex_on_screen(server, browser):
    ctx, page, errors = open_page(browser, server, width=390, height=844, dpr=3, query="?jellytier=low")
    wait_frames(page, 10, timeout=60_000)
    hb = info(page)["heroBox"]
    assert hb["y"] >= 0 and hb["x"] >= 0 and hb["x"] + hb["w"] <= 390 + 2
    ctx.close()


def test_resize_storm_across_breakpoint(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low&jellydebug=1")
    wait_frames(page, 5, timeout=60_000)
    for w in (1440, 700, 1100, 390, 721, 719, 1440):
        page.set_viewport_size({"width": w, "height": 900})
        page.wait_for_timeout(60)
    page.wait_for_timeout(400)
    wait_frames(page, 10, timeout=60_000)
    assert page.locator("canvas#jellyfield").count() == 1
    assert info(page)["drawW"] == 1440
    assert [e for e in errors if "GL error" in e] == []
    ctx.close()


def test_pulse_burst_is_bounded(server, browser):
    ctx, page, errors = open_page(browser, server, query="?jellytier=low&jellydebug=1")
    wait_frames(page, 10, timeout=60_000)
    page.evaluate("for (let i = 0; i < 10; i++) window.Jellyfield.pulse(900, 400, 2.4)")
    wait_frames(page, 30, timeout=60_000)
    assert [e for e in errors if "GL error" in e] == []
    ctx.close()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q -k "places or apex or storm or burst"`
Expected: FAIL. `heroBox` is null.

- [ ] **Step 3: Implement.**

(a) **Camera, pointer, layout, physics and interaction.** Port these from `createJellyfield3D` in `jellyfield.js`, keeping their numbers and comments:
- `perspective`, `mul4`, `computeRay`, `updateCamera`;
- `buildM9`, `setupHero`, `resetHeroNodes`, `heroVerlet`, `sim`;
- `pulse`, `setActivity`, the shells and the flash.

Only these change:
- `setupHero` reads `species.extent.halfWidth`, `.up` and `.down` in place of `BELL_HW`, `SWEPT_UP`/`BELL_UP` and `SWEPT_DN`.
- Chain tables (`chOff`, `chLen`, `chKind`, `chPhi`, `chSeg`, per-node width) are built from `species.chains` × `TIERS[tier].chainScale` at mount and in `rebuildTierMeshes()`. They are preallocated for the **Ultra** count, so a tier change never allocates.
- Margin roots are evaluated by a JS mirror of the species margin. The species provides `marginJS(phi, wave, flutPh) -> [x, y, z]` in bell-local units. That field is added to the contract; the spec's "root: 'margin'" is exactly this.

(b) **Bell renderer.**
- A `Uint32` index grid of `(t, phi)`, with `t` from 0 to 1 and `phi` from 0 to 2π, at `TIERS[tier].bellSeg × bellRings`.
- One program, built from `BELL_VS_HEAD + species.bell.glsl + BELL_VS_MAIN` and `BELL_FS_HEAD + species.bell.glsl + BELL_FS_MAIN`.
- The vertex shader computes position and normal from `bellShape` finite differences, as today's `BELL_VS` does.
- The fragment shader:
  1. `n`, `v`, and Fresnel `F = 0.04 + 0.96 × (1 − |n·v|)^5`.
  2. Refraction: `uv_r = screenUV + (n_view.xy × bellThickness(t, phi) × 0.06)`. Sample `waterTex` at three offsets (`×0.98`, `×1.0`, `×1.02`) for R, G and B dispersion.
  3. Organ march: from the front surface along `refract(v, n, 1/1.02)` in bell-local space, `u_organSteps` steps of `bellThickness × 1.8 / steps`. Accumulate `organField(p, time)`: `emission × transmittance`, with `transmittance *= exp(−absorption × step)`.
  4. `surf = bellSurface(t, phi, n, time)`. Gold gets a metallic specular, a tight Blinn-Phong with `pow(·, 96)` in `palette.gold`.
  5. Wet specular from the key light (0.284, 0.947, 0.151), `pow(·, 180)`.
  6. Output `rgb = mix(refracted × (1 − F) × tint + organs, reflectionOfSurfaceLight, F) + spec + gold`, with `a = coverage` (0.85 front, 0.5 back).

  Two draws: back faces (`gl.cullFace(gl.FRONT)`), then front faces (`gl.cullFace(gl.BACK)`), both with alpha blending.
- **Filaments** (tentacles, fringe): camera-facing strips. The vertex shader receives node positions (per-frame dynamic buffer, preallocated) and expands by the width in **device pixels**: `max(widthPx, 1.0)`. Alpha is multiplied by `widthPx / max(widthPx, 1.0)`, with a 1-px analytic edge fade.
- **Fringe:** instanced. Per-instance `(phi, lengthJitter, phase)`; `count = round(chainCount × fringeScale)`. It bends in the vertex shader by `wave`, `flutPh` and the hero velocity uniform.
- **Frills** (oral arms with `frill`): the ribbon has `across` columns. Edge columns are displaced along the ribbon normal by `amp × sin(freq × s + time × 1.3 + chainSeed)`. Shading is two-sided translucent: `wrap = 0.5 + 0.5 × dot(n, L)`, back-lit by the shafts.

(c) **`info().heroBox`.** After each `setupHero()` and every 30 frames, project 16 margin points (at `wave = 0`) plus the apex through `mVP`. The box is the CSS-px min/max.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest app/tests/test_jellyfield_hd_e2e.py -q`
Expected: PASS. Then run the full suite.

---

### Task 6: Species A — the moon jelly, rebuilt (visual; judged)

**Files:**
- Modify: `app/static/jellyfield-species-moon.js`

**Interfaces:**
- Produces: the full contract object:
  - `bell.glsl` with the four functions;
  - `marginJS`;
  - `chains`: 32 tentacles, 4 frilled oral arms with small amplitude and `goldFerrules: true`, and 320 `fringe` strands;
  - `extent` measured off the render.

- [ ] **Step 1: Start the shape from today's math.** `bellShape` is today's `bellPt(t, phi)`: the 8-lobe margin, contraction `ctr`, lappet flutter and apex crown, from `BELL_FN` in `jellyfield.js`. It is ported to GLSL 3.00 with `wave` and `time` as parameters. `marginJS` is today's `bellMarginLocal` with the `mgX`/`mgY`/`mgZ` globals replaced by a returned array.
- [ ] **Step 2: Fill in the rest to the spec's candidate-A description.**
  - **Surface** (`bellSurface`): 8 inlaid gold meridians (1–1.5 px at 1×, antialiased with `fwidth`), a gold hem at `t ≈ 0.97`, and a faint violet body tint.
  - **Organs** (`organField`): a four-leaf clover of lilac gonads with fine folds (domain-warped `vnoise`), and a radial-canal network made of 16 branching lines at `t` from 0.2 to 0.95.
  - **Rhopalia:** 8 soft emissive points at the lobe notches.
- [ ] **Step 3: Judge it (Task 8 tool) until all these hold:**
  - rim crispness ≥ 1.8× today's;
  - banding runs < 40 px on the backdrop;
  - clipped pixels < 0.05% (the apex star stays gold);
  - phone header contrast ≥ 4.5:1;
  - no GL errors;
  - Ultra desktop frame ≤ 2.5× today's frame time under SwiftShader.

  A reviewer **looks** at the crops: the rim has no kinks or notch, the organs are sharp, the filaments are crisp, and the owner's rules hold.

---

### Task 7: Species B — purple-striped jelly (visual; judged)

**Files:**
- Modify: `app/static/jellyfield-species-striped.js`

**Interfaces:**
- Produces: the full contract object:
  - `bellShape`: a deeper dome than A, apex `y ≈ +0.9`;
  - a margin of 32 lappets with small scallops;
  - `bellSurface`: 16 violet radial stripes, each tapering from apex to margin and slightly wavy, plus a fine gold lappet-edge highlight;
  - `organField`: a subtle gonad glow;
  - `chains`: 8 long dark tentacles (`length ≈ 3.2`, width 0.012 → 0.003), plus 4 oral arms (`length ≈ 3.4`, `frill: {amp: 0.08, freq: 9, across: 8}`, white-lilac);
  - `extent.down` measured to include the arms.

- [ ] **Step 1: Write the contract object to the description above, then judge it** with the same acceptance criteria as Task 6. There is one addition: in phone layout the arms may pass behind the console card, but **the bell alone** must fit the band.

---

### Task 8: Judge tooling

**Files:**
- Create: `tools/jelly_capture.py` (frames and crops) and `tools/jelly_judge.py` (metrics)
- Test: `app/tests/test_jelly_judge.py` (metric unit tests on synthetic images)

**Interfaces:**
- `jelly_capture.py OUT_DIR --variants classic,moon,striped`
  - Starts the app with a stubbed Jellyfin (the Task 1 fixture logic).
  - Captures `{variant}-{1440x900@1,1440x900@2,390x844@3,3840x2160@1}.png` after 8 s, via `?jelly=<variant>`.
  - Writes `{variant}-crop-{rim,organs,filaments}.png` from `info().heroBox`.
- `jelly_judge.py OUT_DIR`
  - Prints JSON per image: `rim_crispness`, `max_flat_run`, `clipped_pct`, and `header_contrast` (phone frames only).
  - `rim_crispness` is the mean Sobel magnitude on a 6-px band around the bell silhouette. The silhouette is taken from the `heroBox` ellipse: the brightest-gradient ring within the box.
  - `header_contrast` is the WCAG ratio of the `.wordmark` text's colour against the 95th-percentile luminance of the creature pixels behind the header box.

- [ ] **Step 1: Write the failing tests** (`app/tests/test_jelly_judge.py`):

```python
import numpy as np
from PIL import Image

import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "jelly_judge", pathlib.Path(__file__).resolve().parents[2] / "tools" / "jelly_judge.py")
jj = importlib.util.module_from_spec(spec); spec.loader.exec_module(jj)


def test_flat_run_detects_banding():
    smooth = np.tile(np.linspace(10, 60, 400)[:, None], (1, 50))
    banded = np.round(smooth / 10) * 10
    assert jj.max_flat_run(banded.astype(np.uint8)[:, 5]) > jj.max_flat_run(
        (smooth + np.random.default_rng(0).integers(-1, 2, smooth.shape)).astype(np.uint8)[:, 5])


def test_clipped_pct():
    img = np.zeros((10, 10, 3), np.uint8); img[0, :, :] = 255
    assert jj.clipped_pct(img) == 10.0


def test_contrast_ratio():
    assert round(jj.contrast_ratio((255, 255, 255), (0, 0, 0)), 1) == 21.0
    assert round(jj.contrast_ratio((118, 118, 118), (255, 255, 255)), 1) == 4.5
```

- [ ] **Step 2: Run them to verify they fail.** Expected: `FileNotFoundError` for `tools/jelly_judge.py`.
- [ ] **Step 3: Implement** `max_flat_run(col)`, `clipped_pct(img)`, `relative_luminance(rgb)`, `contrast_ratio(a, b)` (WCAG 2.x), `rim_crispness(img, box)`, and the CLI. Then implement `jelly_capture.py` with the Task 1 fixture logic, using Playwright and the system-Chrome fallback.
- [ ] **Step 4: Run them to verify they pass.**

---

### Task 9: Bake-off rounds and the owner's pick

- [ ] **Step 1:** Run `tools/jelly_capture.py` for `classic`, `moon` and `striped`, then `tools/jelly_judge.py`.
- [ ] **Step 2:** Refinement round 1 for each candidate (parallel agents, one per species file). Feed each candidate its judge JSON and crops plus the opinion critique. Re-capture.
- [ ] **Step 3:** Refinement round 2, the same way.
- [ ] **Step 4:** Build the comparison page, a private Artifact:
  - rows per size (desktop 1×, 2×, phone 3×, 4K), plus crop rows;
  - columns *Today / A — Moon jelly / B — Purple-striped*;
  - each column shows its judge numbers.

  Publish it and ask the owner to pick.

---

### Task 10: Finalize

**Files:**
- Delete: the losing `app/static/jellyfield-species-*.js`, and its `<script>` tags in both templates.
- Modify: `app/static/jellyfield.js`: delete `createJellyfield3D`, `selTry3D` and the `classic` param path. The order becomes HD → 2D.
- Modify: `app/static/jellyfield-hd.js`: the `jelly` param is removed (one species). `jellytier`, `jellyslow`, `jellydebug` and `jellyldr` stay for tests.
- Modify: `app/tests/test_jellyfield_hd_e2e.py`: drop `test_species_and_classic_params`, and assert `species` is the winner. `app/tests/test_template.py`: update the script-order test.
- Modify: `README.md`: one short paragraph under Stack about the HD engine, its tiers and fallbacks.

- [ ] **Step 1:** Make the deletions and edits above.
- [ ] **Step 2:** Full verification:
  - `python3 -m pytest app/tests -q` (all, e2e included);
  - `node --check` on every `app/static/*.js`;
  - the CI inline-script check;
  - `JELLY_SHOTS` frames looked at, at all four sizes.
- [ ] **Step 3:** An adversarial review of the whole diff: lifecycle, context loss, per-frame allocation, precision, fallbacks and layout. Fix what it confirms, then re-verify.
- [ ] **Step 4:** One commit, then push to master. Then watch CI to green and the GHCR image published.
