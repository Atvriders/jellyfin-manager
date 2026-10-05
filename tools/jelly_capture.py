#!/usr/bin/env python3
"""Capture judged frames of the Jellyfield HD background (spec: "Bake-off").

    python3 tools/jelly_capture.py OUT_DIR [--variants striped]
        [--sizes 1440x900@1,1440x900@2,390x844@3,3840x2160@1]
        [--probe-sizes 1440x900@1:low]
        [--settle 8] [--min-frames 24] [--at-frame N] [--max-wait 900]

Starts the app in-process with a stubbed Jellyfin (the e2e fixture logic),
opens the page at each size in real Chrome on SwiftShader, waits for the
creature to settle, and writes per variant the files below. The engine has
one species since the bake-off (`striped`), so a variant is the name the
files carry: the page mounts whatever species ships, and the capture warns
when the mounted species is not the variant's name. The bake-off's other
variants cannot be captured any more: `classic` was the retired 3D engine
and `moon` the retired species (both rejected up front).

  {variant}-{WxH@dpr}.png          the page frame (device px = CSS x dpr)
  {variant}-{WxH@dpr}.json         sidecar for tools/jelly_judge.py: engine,
                                   species, tier, heroBox (CSS px), the water
                                   strip, frame_ms, draw calls, console
                                   errors, the frame indices of both frames
  {variant}-{WxH@dpr}-bare.png     a LATER frame with the DOM column hidden
                                   (creature + water only): the judge measures
                                   this one — clipping without the page's
                                   white headings, the rim without header text
                                   over it, and it is the ruling's text-hidden
                                   frame for the phone header contrast. It is
                                   several SwiftShader frames after the page
                                   frame (the page screenshot and the DOM
                                   reads cost frames: +6..+14 in the
                                   baseline), so it is a different breath
                                   phase; the sidecar records both indices
                                   (frames = page, frames_bare).
  {variant}-crop-{rim,organs,filaments}.png   1:1 crops of the 1440x900@2
                                   BARE frame (the frame the numbers come
                                   from), framed from heroBox
  {variant}-{WxH@dpr}-low.png (+ -bare.png, .json)   the Low-tier cost probe
                                   (--probe-sizes): its frame_ms is what the
                                   cost gate reads

Tiers (controller ruling): each size is pinned to its natural starting tier,
because an unpinned SwiftShader page steps down mid-capture — desktop sizes
?jellytier=ultra, the phone ?jellytier=high. A size spec may pin another tier
explicitly, `WxH@dpr:tier`, which also names the files
({variant}-{WxH@dpr}-{tier}.png); the probes use that.

Timing (controller ruling): the page frame is captured once BOTH the settle
time (>= 8 s from navigation, the brief's number) and the frame count have
been reached, then at the first RAF tick after that. Under SwiftShader the
creature clock is frame-quantized (the engine clamps dt at 50 ms/frame and
render at a few fps), so the breath phase is frames x 0.05 s / species.breath:
the sidecar records `frames` (page) and `frames_bare` for each capture, and
--at-frame N pins every capture's page frame to the same index (the species
breathes at 4.5 s, so equal frame indices are equal phases).

Dependencies (tooling only, not app requirements): playwright, Pillow.
"""

import argparse
import atexit
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from jelly_judge import crop_rects  # noqa: E402  (one definition of the crop / detail rects)

DEFAULT_SIZES = "1440x900@1,1440x900@2,390x844@3,3840x2160@1"
PROBE_SIZES = "1440x900@1:low"      # the Low-tier cost probe
CROP_SIZE = "1440x900@2"
GL_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
           "--enable-precise-memory-info"]
PHONE_MAX_CSS_W = 720
# the bake-off's other variants: nothing serves them any more
REMOVED_VARIANTS = {
    "classic": "the old 3D engine was retired after the bake-off",
    "moon": "the moon species was retired after the bake-off",
}

# frame counter + draw-call counter, installed before any page script. RAF is
# wrapped so the count is the page's frame index whichever engine won (the 2D
# fallback has no info().frames); the GL draw entry points are wrapped so draw
# calls per frame come out of the same two counters.
COUNTERS_JS = """(() => {
  window.__raf = 0; window.__draws = 0;
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => raf((t) => { window.__raf++; return cb(t); });
  for (const P of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    if (!P) continue;
    for (const k of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
      const f = P.prototype[k]; if (!f) continue;
      P.prototype[k] = function () { window.__draws++; return f.apply(this, arguments); };
    }
  }
})()"""

# The phone header text (controller ruling): computed colour(s) + CSS box per
# element. The wordmark is a gradient clipped to its text, so both gradient
# stops are its colours; the others read their computed color.
HEADER_JS = """() => {
  const rgb = (s) => (s.match(/rgba?\\([^)]*\\)/g) || []).map((m) => {
    const p = m.replace(/rgba?\\(|\\)/g, '').split(',').map((v) => parseFloat(v));
    return [Math.round(p[0]), Math.round(p[1]), Math.round(p[2])];
  });
  const box = (el) => { const r = el.getBoundingClientRect();
    return { x: r.left + (window.pageXOffset || 0), y: r.top + (window.pageYOffset || 0), w: r.width, h: r.height }; };
  const out = [];
  const add = (name, sel, mode) => {
    const el = document.querySelector(sel); if (!el) return;
    const cs = getComputedStyle(el);
    const colors = mode === 'gradient' ? rgb(cs.backgroundImage) : rgb(cs.color);
    if (colors.length) out.push({ name, selector: sel, colors, box: box(el) });
  };
  add('tick_label', 'header.station .tick-label', 'color');
  add('wordmark', 'header.station .wordmark', 'gradient');
  add('eyebrow', 'header.station .brand-text .eyebrow', 'color');
  add('session', 'header.station .session-who', 'color');
  add('session_user', 'header.station .session-user', 'color');
  return out;
}"""

# the DOM column (header text, glass panels, headings) out of the way; the
# canvas and the page vignette stay, and visibility keeps the layout (the
# engine measured .column at mount, so the dim band is unchanged)
HIDE_COLUMN_JS = """() => {
  const el = document.querySelector('main.column'); if (el) el.style.visibility = 'hidden';
  return true;
}"""

# CSS-px facts the judge needs: the text column's box and the water-only strip
# beside it (desktop: left of .column, clear of the rail; phone: the column
# spans the width, so its own left padding is the strip).
LAYOUT_JS = """() => {
  const col = document.querySelector('.column');
  const r = col ? col.getBoundingClientRect() : null;
  const pad = col ? parseFloat(getComputedStyle(col).paddingLeft) || 0 : 0;
  const x1 = r ? (r.left >= 40 ? r.left : r.left + pad) : Math.min(40, window.innerWidth);
  return { column: r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null,
           water_strip: { x0: 0, x1: Math.max(0, x1) } };
}"""


# ------------------------------------------------------------- the server

class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def start_server():
    """The app with a stubbed Jellyfin, as app/tests/test_jellyfield_hd_e2e.py
    runs it: signed in, an empty scan history in a throwaway DATA_DIR, every
    Jellyfin GET answering an empty list."""
    data_dir = tempfile.mkdtemp(prefix="jfm-capture-data-")
    atexit.register(shutil.rmtree, data_dir, ignore_errors=True)
    os.environ["DATA_DIR"] = data_dir
    os.environ.setdefault("SECRET_KEY", "jelly-capture-only")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)   # no per-request lines in the capture log
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    from werkzeug.serving import make_server
    import app as app_module
    from history import ScanHistory
    app_module.authenticated = lambda: True
    app_module.history = ScanHistory(os.path.join(data_dir, "h.json"))
    app_module.JELLYFIN_URL = "http://jellyfin.test"
    app_module.JELLYFIN_API_KEY = "k"
    app_module.requests.get = lambda *a, **kw: FakeResponse(payload=[])
    srv = make_server("127.0.0.1", 0, app_module.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/"


# ------------------------------------------------------------ the browser

def launch(pw, chrome=None):
    attempts = [{}] + [{"executable_path": p} for p in
                       (chrome, os.environ.get("CHROME_PATH"), "/usr/bin/google-chrome", "/usr/bin/chromium")
                       if p and os.path.exists(p)]
    errors = []
    for kw in attempts:
        try:
            return pw.chromium.launch(args=GL_ARGS, **kw)
        except Exception as exc:
            errors.append(str(exc).splitlines()[0])
    raise SystemExit("no usable chromium: " + " | ".join(errors))


def wait_for(page, expr, timeout_s):
    """Polled from here: the app's CSP has no 'unsafe-eval', which breaks
    page.wait_for_function; page.evaluate of a plain expression is exempt."""
    deadline = time.monotonic() + timeout_s
    while not page.evaluate(expr):
        if time.monotonic() > deadline:
            raise TimeoutError(f"gave up after {timeout_s} s waiting for: {expr}")
        time.sleep(0.05)


def parse_size(s):
    """'WxH@dpr' or 'WxH@dpr:tier' -> (w, h, dpr, tier or None)."""
    spec, _, tier = s.partition(":")
    wh, dpr = spec.split("@")
    w, h = wh.split("x")
    if tier and tier not in ("ultra", "high", "low"):
        raise SystemExit(f"bad tier in size spec {s!r}")
    return int(w), int(h), int(dpr), (tier or None)


def tier_for(css_w, forced=None):
    if forced:
        return forced
    return "high" if css_w <= PHONE_MAX_CSS_W else "ultra"


def query_for(css_w, forced=None):
    """The page query: only the tier pin. The engine has one species, so
    there is no species parameter to send."""
    return f"?jellytier={tier_for(css_w, forced)}"


def check_variants(variants):
    """The bake-off's other variants fail loudly instead of capturing the
    shipped species under a misleading file name."""
    bad = [f"{v!r} ({REMOVED_VARIANTS[v]})" for v in variants if v in REMOVED_VARIANTS]
    if bad:
        raise SystemExit("cannot capture " + ", ".join(bad) + "; the one HD species is 'striped'")
    if not variants:
        raise SystemExit("no variants to capture")


def counters(page):
    return page.evaluate("({ t: performance.now(), raf: window.__raf, draws: window.__draws })")


def capture_one(browser, base_url, variant, size, out_dir, settle_s, min_frames, at_frame, max_wait_s, log):
    w, h, dpr, forced = parse_size(size)
    size_name = f"{w}x{h}@{dpr}"
    stem = f"{variant}-{size_name}" + (f"-{forced}" if forced else "")
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=dpr,
                              reduced_motion="no-preference")
    try:
        ctx.add_init_script(COUNTERS_JS)
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error"
                and not m.location.get("url", "").endswith("/favicon.ico") else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        t_nav = time.monotonic()
        page.goto(base_url + query_for(w, forced))
        wait_for(page, "window.Jellyfield && window.Jellyfield.info && window.Jellyfield.info().engine !== ''", 60)
        info = page.evaluate("window.Jellyfield.info()")
        if info["engine"] != "hd":
            log(f"  WARNING {variant} {size}: engine is {info['engine']!r}, wanted 'hd'")
        elif info.get("species") != variant:
            log(f"  WARNING {variant} {size}: the page mounted species {info.get('species')!r}; "
                f"the files are named {variant!r}")
        # settle: >= settle_s since navigation AND >= min_frames (or the pinned frame index)
        target = at_frame if at_frame else min_frames
        deadline = time.monotonic() + max_wait_s
        while True:
            c = counters(page)
            elapsed = time.monotonic() - t_nav
            if c["raf"] >= target and elapsed >= settle_s:
                break
            if time.monotonic() > deadline:
                log(f"  WARNING {variant} {size}: settle timed out at {c['raf']} frames / {elapsed:.0f} s")
                break
            time.sleep(0.05)
        # cost: wall time per RAF and draw calls per RAF over the next few frames.
        # SwiftShader-relative ONLY (a software rasterizer, and the interval
        # includes this loop's evaluate polling): compare variants captured the
        # same way on the same idle machine, never against a real GPU.
        c0 = counters(page)
        wait_for(page, f"window.__raf >= {c0['raf'] + 4}", max(30, max_wait_s / 4))
        c1 = counters(page)
        n = max(1, c1["raf"] - c0["raf"])
        frame_ms = (c1["t"] - c0["t"]) / n
        draws = (c1["draws"] - c0["draws"]) / n
        # capture right after the next frame lands
        r = counters(page)["raf"]
        wait_for(page, f"window.__raf > {r}", max(30, max_wait_s / 4))
        png = page.screenshot()
        c_shot = counters(page)
        info = page.evaluate("window.Jellyfield.info()")
        layout = page.evaluate(LAYOUT_JS)
        hero = info.get("heroBox")   # None only if HD fell back to 2D (warned above)
        (out_dir / f"{stem}.png").write_bytes(png)
        meta = {
            "variant": variant, "size": size_name, "css": {"w": w, "h": h}, "dpr": dpr,
            "tier": tier_for(w, forced), "engine": info.get("engine"), "species": info.get("species"),
            "tier_reported": info.get("tier"), "hdr": info.get("hdr"), "renderScale": info.get("renderScale"),
            "drawW": info.get("drawW"), "drawH": info.get("drawH"),
            "heroBox": hero, "heroBoxSource": "info" if hero else None,
            "column": layout["column"], "water_strip": layout["water_strip"],
            "frames": c_shot["raf"], "engine_frames": info.get("frames"),
            "elapsed_s": round(time.monotonic() - t_nav, 2),
            "frame_ms": round(frame_ms, 1), "draw_calls_per_frame": round(draws, 1),
            "errors": errors,
        }
        if w <= PHONE_MAX_CSS_W:
            meta["header"] = page.evaluate(HEADER_JS)
        # the bare frame (DOM column hidden): what the judge measures and what
        # the crops are cut from. It lands several frames after the page frame
        # (the screenshot above and the evaluates cost SwiftShader frames), so
        # both indices are recorded: frames (page) and frames_bare.
        page.evaluate(HIDE_COLUMN_JS)
        r = counters(page)["raf"]
        wait_for(page, f"window.__raf > {r}", max(30, max_wait_s / 4))
        png_bare = page.screenshot()
        meta["frames_bare"] = counters(page)["raf"]
        (out_dir / f"{stem}-bare.png").write_bytes(png_bare)
        (out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n")
        log(f"  {stem}: engine={meta['engine']} species={meta['species']} tier={meta['tier_reported']} "
            f"frames page={meta['frames']} bare={meta['frames_bare']} {meta['elapsed_s']} s  "
            f"{frame_ms:.0f} ms/frame  {draws:.0f} draws/frame"
            f"{'  ERRORS: ' + str(len(errors)) if errors else ''}")
        return meta, png_bare
    finally:
        ctx.close()


# ---------------------------------------------------------------- crops

def write_crops(png_bare, meta, variant, out_dir):
    """1:1 crops (rects from jelly_judge.crop_rects) of the BARE frame — the
    same frame the judge's numbers come from, so crops and metrics show one
    breath phase."""
    from PIL import Image
    img = Image.open(io.BytesIO(png_bare)).convert("RGB")
    rects = crop_rects(meta["heroBox"], meta["dpr"], img.width, img.height)
    for name, (x0, y0, x1, y1) in rects.items():
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        img.crop((x0, y0, x1, y1)).save(out_dir / f"{variant}-crop-{name}.png")
    meta["crops"] = {k: list(v) for k, v in rects.items()}
    meta["crops_from"] = f"{variant}-{meta['size']}-bare.png"
    (out_dir / f"{variant}-{meta['size']}.json").write_text(json.dumps(meta, indent=2) + "\n")


# ----------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir")
    ap.add_argument("--variants", default="striped",
                    help="file-name labels for the captures (the page always mounts the one shipped species)")
    ap.add_argument("--sizes", default=DEFAULT_SIZES)
    ap.add_argument("--probe-sizes", default=PROBE_SIZES,
                    help="extra pinned-tier captures (cost probes); '' for none")
    ap.add_argument("--settle", type=float, default=8.0, help="minimum seconds from navigation to capture")
    ap.add_argument("--min-frames", type=int, default=24, help="minimum frames rendered before capture")
    ap.add_argument("--at-frame", type=int, default=0, help="capture at this frame index (phase-matched)")
    ap.add_argument("--max-wait", type=float, default=900.0, help="seconds before a settle gives up")
    ap.add_argument("--chrome", help="chromium executable (default: playwright's, then system Chrome)")
    ap.add_argument("--crop-size", default=CROP_SIZE, help=f"the size the crops are cut from (default {CROP_SIZE})")
    args = ap.parse_args(argv)

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    check_variants(variants)
    sizes = [s.strip() for s in args.sizes.split(",") if s.strip()]
    probes = [s.strip() for s in args.probe_sizes.split(",") if s.strip()]
    for s in sizes + probes:
        parse_size(s)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def log(msg):
        print(msg, flush=True)

    from playwright.sync_api import sync_playwright
    srv, base_url = start_server()
    log(f"app at {base_url}")
    try:
        with sync_playwright() as pw:
            browser = launch(pw, args.chrome)
            try:
                for variant in variants:
                    log(f"{variant}:")
                    for size in sizes + probes:
                        meta, png_bare = capture_one(browser, base_url, variant, size, out_dir, args.settle,
                                                     args.min_frames, args.at_frame, args.max_wait, log)
                        if size == args.crop_size and meta.get("heroBox"):
                            write_crops(png_bare, meta, variant, out_dir)
            finally:
                browser.close()
    finally:
        srv.shutdown()
    log(f"wrote {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
