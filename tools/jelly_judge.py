#!/usr/bin/env python3
"""Measured judges for the Jellyfield bake-off, computed from the PNGs that
tools/jelly_capture.py wrote (spec: "Bake-off", "Measured judges").

    python3 tools/jelly_judge.py OUT_DIR [--debug-dir DIR] [--quiet]

Prints one JSON object keyed by frame file name (and writes the same to
OUT_DIR/judge.json). Per frame:

  rim_crispness     mean Sobel magnitude (gamma-space luminance, 0..255) on a
                    6 px band around the bell silhouette. The silhouette is
                    found inside the heroBox ellipse: along each radial ray the
                    brightest-gradient sample, median-smoothed around the ring.
                    Relative: compare frames of the same size only. It rises
                    with the edge's contrast and falls once the transition is
                    wider than the band; the two companions below separate
                    sharpness from contrast:
  rim_peak          mean per-ray peak Sobel magnitude across the silhouette
                    (a crisp edge concentrates its step into one or two px).
  rim_width_px      mean full width at half that peak along the ray, in px
                    (~2 for a crisp antialiased edge; soft edges spread).
  max_flat_run      banding — the longest run of equal channel-sum values down
                    a water-only column, max over columns sampled in the strip
                    beside the text column (from the sidecar's water_strip).
  distinct_levels   banding — the fewest distinct channel-sum values in one of
                    those columns (a posterized gradient has few).
  clipped_pct       % of pixels with any channel at 255.
  clipped_white_pct % of pixels with all three channels at 255.
  clipped_pct_page  the same over the page frame, DOM included (reference).
  header_contrast   phone frames only: the min WCAG ratio between each header
                    text element's colour(s) and the 95th-percentile relative
                    luminance of the pixels behind its box, read from the
                    second screenshot with the DOM column hidden (*-bare.png).
                    Per-element ratios are in header_contrast_elements.
  detail_energy     variance of the 3x3 Laplacian of the luminance inside the
                    organ crop rect derived from heroBox (detail_energy_rect,
                    image px), per pixel: fine structure in the bell / organs.
  frame_ms, draw_calls_per_frame, tier, engine, species, errors — carried
                    from the capture sidecar (cost is SwiftShader-relative).
  frames_page / frames_bare — the RAF indices of the page frame and of the
                    measured bare frame (see below).

Every pixel metric is measured on the *-bare.png frame when the capture wrote
one: the DOM column hidden, so creature and water only — the page's white
headings are not "clipping" and the header text does not sit on the rim.
The bare frame is NOT the frame after the page frame: the page screenshot
and the DOM reads between the two cost several SwiftShader frames (+6 to
+14 in the baseline), so the two are different breath phases. The capture
therefore cuts the crops from the bare frame too, and both indices are
reported; `measured_on` says which frame the numbers come from.

A {variant}-{size}-low.png is the Low-tier cost probe (its frame_ms is what
the cost gate reads); it is judged like any other frame.

The judge needs numpy and Pillow (not app dependencies: tooling only).
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# {variant}-{W}x{H}@{dpr}.png, or the pinned-tier cost probe {…}@{dpr}-low.png;
# never the *-bare.png companions, the crops or the overlays
FRAME_RE = re.compile(r"^(?P<variant>[a-z0-9]+)-(?P<w>\d+)x(?P<h>\d+)@(?P<dpr>\d+)(?:-(?P<tier>ultra|high|low))?\.png$")
BAND_HALF_PX = 3          # the 6 px band around the silhouette
PHONE_MAX_CSS_W = 720     # the layout's phone breakpoint: header_contrast applies below it


# ------------------------------------------------------------------ basics

def luminance(img):
    """Gamma-space luminance (0..255 float) of an HxWx3 uint8 array; a 2-D
    array is returned as float unchanged."""
    a = np.asarray(img)
    if a.ndim == 2:
        return a.astype(np.float64)
    a = a.astype(np.float64)
    return 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]


def _srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64) / 255.0
    return np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def relative_luminance(rgb):
    """WCAG 2.x relative luminance of one sRGB colour (0..255 per channel)."""
    r, g, b = _srgb_to_linear(np.asarray(rgb, dtype=np.float64)[:3])
    return float(0.2126 * r + 0.7152 * g + 0.0722 * b)


def relative_luminance_image(img):
    """Per-pixel WCAG relative luminance of an HxWx3 uint8 array."""
    lin = _srgb_to_linear(np.asarray(img)[..., :3])
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


def contrast_ratio(a, b):
    """WCAG contrast ratio between two colours (order-independent, >= 1)."""
    return contrast_from_luminance(relative_luminance(a), relative_luminance(b))


def contrast_from_luminance(la, lb):
    hi, lo = (la, lb) if la >= lb else (lb, la)
    return float((hi + 0.05) / (lo + 0.05))


def max_flat_run(col):
    """Longest run of equal consecutive values in a 1-D array."""
    col = np.asarray(col).ravel()
    if col.size == 0:
        return 0
    change = np.flatnonzero(np.diff(col) != 0)
    edges = np.concatenate(([-1], change, [col.size - 1]))
    return int(np.diff(edges).max())


def distinct_levels(col):
    return int(np.unique(np.asarray(col).ravel()).size)


def clipped_pct(img):
    """% of pixels with ANY channel at 255 (the brief's "% of pixels at channel 255")."""
    a = np.asarray(img)
    if a.ndim == 3:
        hit = (a[..., :3] == 255).any(axis=2)
    else:
        hit = a == 255
    return float(100.0 * hit.sum() / hit.size)


def clipped_white_pct(img):
    """% of pixels with all three channels at 255: the apex star clipping to white."""
    a = np.asarray(img)
    hit = (a[..., :3] == 255).all(axis=2) if a.ndim == 3 else (a == 255)
    return float(100.0 * hit.sum() / hit.size)


# ------------------------------------------------------------- rim crispness

def sobel_magnitude(lum):
    """Sobel gradient magnitude of a 2-D float image (edge-replicated)."""
    p = np.pad(np.asarray(lum, dtype=np.float64), 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    return np.hypot(gx, gy)


def _sample(mag, xs, ys):
    """Nearest-pixel samples; anything outside the image reads 0."""
    h, w = mag.shape
    xi = np.rint(xs).astype(int)
    yi = np.rint(ys).astype(int)
    ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
    out = np.zeros(xs.shape, dtype=np.float64)
    out[ok] = mag[yi[ok], xi[ok]]
    return out


def _running_median(v, k=7):
    p = np.pad(v, k // 2, mode="edge")
    idx = np.arange(v.size)[:, None] + np.arange(k)[None, :]
    return np.median(p[idx], axis=1)


# The exumbrella against open water: image angles (y down) from 20 deg below
# the left horizontal, over the apex, to 20 deg below the right horizontal.
# Below that the outline is the margin against its own fringe and strands,
# where "the silhouette" is ambiguous (the heroBox bottom is the root ring);
# the rim crop is what a human reads there.
UPPER_ARC = (math.pi - 0.35, 2 * math.pi + 0.35)


def rim_silhouette(lum, box, r_lo=0.35, r_hi=1.25, angles=None, arc=UPPER_ARC, weak=0.25):
    """The bell silhouette inside the heroBox ellipse, as an (N, 2) array of
    (x, y) image points along `arc`. For each ray from the ellipse centre the
    sample with the strongest gradient between r_lo and r_hi of the ellipse
    radius is the edge; the radii are median-smoothed along the arc so one
    mote, strand or organ edge cannot pull a single ray inward. Rays whose
    peak is under `weak` x the median peak (the frame edge cutting the bell,
    a ray that found only water) are dropped."""
    x, y, w, h = box
    mag = sobel_magnitude(lum)
    cx, cy, a, b = x + w / 2.0, y + h / 2.0, max(w / 2.0, 1.0), max(h / 2.0, 1.0)
    span = arc[1] - arc[0]
    if angles is None:
        perimeter = math.pi * (3 * (a + b) - math.sqrt((3 * a + b) * (a + 3 * b)))
        angles = int(max(180, min(2000, perimeter * span / (2 * math.pi) / 1.5)))
    th = np.linspace(arc[0], arc[1], angles)
    n_r = int(max(16, (r_hi - r_lo) * max(a, b)))
    rs = np.linspace(r_lo, r_hi, n_r)
    xs = cx + rs[None, :] * a * np.cos(th)[:, None]
    ys = cy + rs[None, :] * b * np.sin(th)[:, None]
    prof = _sample(mag, xs, ys)
    # a 3-tap smooth along the ray so a one-sample spike does not win
    sm = prof.copy()
    sm[:, 1:-1] = (prof[:, :-2] + prof[:, 1:-1] + prof[:, 2:]) / 3.0
    peaks = sm.max(axis=1)
    r_star = _running_median(rs[np.argmax(sm, axis=1)], 7)
    keep = peaks >= weak * np.median(peaks)
    if keep.sum() >= 8:
        th, r_star = th[keep], r_star[keep]
    return np.stack([cx + r_star * a * np.cos(th), cy + r_star * b * np.sin(th)], axis=1)


def rim_band_mask(shape, pts, half=BAND_HALF_PX):
    """Boolean mask of pixels within `half` px of the silhouette polyline."""
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    r = int(math.ceil(half))
    dy, dx = np.mgrid[-r:r + 1, -r:r + 1]
    disc = (dx * dx + dy * dy) <= half * half
    oy, ox = np.nonzero(disc)
    oy, ox = oy - r, ox - r
    xi = np.rint(pts[:, 0]).astype(int)
    yi = np.rint(pts[:, 1]).astype(int)
    px = (xi[:, None] + ox[None, :]).ravel()
    py = (yi[:, None] + oy[None, :]).ravel()
    ok = (px >= 0) & (px < w) & (py >= 0) & (py < h)
    mask[py[ok], px[ok]] = True
    return mask


def rim_profiles(mag, pts, box, reach=12.0, step=0.5):
    """Gradient magnitude along each silhouette point's ray, from -reach to
    +reach image px around the point: an (N, K) array."""
    x, y, w, h = box
    cx, cy = x + w / 2.0, y + h / 2.0
    d = pts - np.array([cx, cy])
    n = np.linalg.norm(d, axis=1)
    n[n == 0] = 1.0
    u = d / n[:, None]
    s = np.arange(-reach, reach + step / 2, step)
    xs = pts[:, 0:1] + s[None, :] * u[:, 0:1]
    ys = pts[:, 1:2] + s[None, :] * u[:, 1:2]
    return _sample(mag, xs, ys), step


def rim_sharpness(mag, pts, box):
    """(peak, width_px): the mean per-ray peak Sobel magnitude, and the mean
    full width at half that peak along the ray, in px. A crisp antialiased
    edge peaks high and is ~2 px wide; a soft one peaks low and spreads."""
    prof, step = rim_profiles(mag, pts, box)
    peaks = prof.max(axis=1)
    widths = []
    for row, pk in zip(prof, peaks):
        if pk <= 0:
            continue
        i = int(np.argmax(row))
        half = pk * 0.5
        lo = i
        while lo > 0 and row[lo - 1] >= half:
            lo -= 1
        hi = i
        while hi < row.size - 1 and row[hi + 1] >= half:
            hi += 1
        widths.append((hi - lo + 1) * step)
    return float(peaks.mean()), (float(np.mean(widths)) if widths else 0.0)


def rim_metrics(img, box, debug_path=None):
    """{'crispness', 'peak', 'width_px'} for the bell silhouette found inside
    `box` = (x, y, w, h) in image pixels: crispness is the brief's mean Sobel
    magnitude on the 6 px band; peak and width_px are the companion
    sharpness numbers (see rim_sharpness). With debug_path, writes the frame
    with the band painted magenta so a human can check the ring."""
    lum = luminance(img)
    mag = sobel_magnitude(lum)
    pts = rim_silhouette(lum, box)
    mask = rim_band_mask(lum.shape, pts)
    if not mask.any():
        return {"crispness": 0.0, "peak": 0.0, "width_px": 0.0}
    peak, width = rim_sharpness(mag, pts, box)
    if debug_path is not None:
        a = np.asarray(img)
        dbg = (np.repeat(a[..., None], 3, axis=2) if a.ndim == 2 else a[..., :3]).copy()
        dbg[mask] = (255, 0, 255)
        x, y, w, h = [int(round(v)) for v in box]
        dbg[max(y, 0):y + h, max(x, 0):x + 1] = (0, 255, 0)
        dbg[max(y, 0):y + h, max(x + w - 1, 0):x + w] = (0, 255, 0)
        dbg[max(y, 0):y + 1, max(x, 0):x + w] = (0, 255, 0)
        dbg[max(y + h - 1, 0):y + h, max(x, 0):x + w] = (0, 255, 0)
        Image.fromarray(dbg).save(debug_path)
    return {"crispness": float(mag[mask].mean()), "peak": peak, "width_px": width}


def rim_crispness(img, box, debug_path=None):
    """Mean Sobel magnitude on the 6 px band around the bell silhouette (the
    brief's definition). Measures how much luminance the edge steps across
    the band; a transition wider than the band lowers it."""
    return rim_metrics(img, box, debug_path)["crispness"]


# ------------------------------------------------------------ detail energy

def laplacian(lum):
    """3x3 four-neighbour Laplacian of a 2-D float image (edge-replicated):
    zero on flat fields and linear ramps, large on fine structure."""
    p = np.pad(np.asarray(lum, dtype=np.float64), 1, mode="edge")
    return p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] - 4.0 * p[1:-1, 1:-1]


def detail_energy(img, rect):
    """Variance of the Laplacian of the luminance inside rect = (x0, y0, x1,
    y1) in image px, i.e. per pixel: how much fine structure the region holds
    (a blur lowers it; a flat or smoothly shaded region scores ~0)."""
    x0, y0, x1, y1 = [int(v) for v in rect]
    lap = laplacian(luminance(img))[max(0, y0):y1, max(0, x0):x1]
    if lap.size == 0:
        return 0.0
    return float(lap.var())


def crop_rects(hb, dpr, img_w, img_h):
    """Crop boxes in image px from the CSS heroBox (x, y, w, h): the rim (the
    lower bell and its margin, full width), the organs (the bell's interior;
    also the detail_energy region) and the filaments (the drape below the
    margin). Clamped to the frame. Shared with tools/jelly_capture.py."""
    x, y, w, h = hb["x"] * dpr, hb["y"] * dpr, hb["w"] * dpr, hb["h"] * dpr
    want = {
        "rim": (x - 0.08 * w, y + 0.50 * h, x + 1.08 * w, y + 1.15 * h),
        "organs": (x + 0.15 * w, y + 0.18 * h, x + 0.85 * w, y + 0.98 * h),
        "filaments": (x - 0.10 * w, y + 0.85 * h, x + 1.10 * w, y + 0.85 * h + 1.3 * h),
    }
    out = {}
    for k, (x0, y0, x1, y1) in want.items():
        out[k] = (int(max(0, round(x0))), int(max(0, round(y0))),
                  int(min(img_w, round(x1))), int(min(img_h, round(y1))))
    return out


# ------------------------------------------------------------------ banding

def water_banding(img, strip, columns=8):
    """Flat-run and level statistics down water-only columns. `strip` is the
    (x0, x1) image-px range that holds nothing but water (left of the text
    column); up to `columns` evenly spaced columns inside it are read."""
    a = np.asarray(img)
    x0, x1 = int(max(0, strip[0])), int(min(a.shape[1], strip[1]))
    if x1 - x0 < 1:
        return {"max_flat_run": None, "distinct_levels": None, "columns": []}
    xs = sorted(set(int(v) for v in np.linspace(x0 + 2, x1 - 3, columns))) if x1 - x0 > 6 \
        else list(range(x0, x1))
    runs, levels = [], []
    for x in xs:
        col = a[:, x, :3].astype(int).sum(axis=1)
        runs.append(max_flat_run(col))
        levels.append(distinct_levels(col))
    return {"max_flat_run": int(max(runs)), "distinct_levels": int(min(levels)), "columns": xs}


# --------------------------------------------------------- header contrast

def header_contrast(frame_notext, elements, dpr):
    """Per-element and minimum WCAG ratios: each element's colour(s) against
    the 95th-percentile relative luminance of the pixels behind its CSS box
    (scaled by dpr into the frame taken with the text hidden). Text shadows
    and the halo are ignored: conservative."""
    lum = relative_luminance_image(frame_notext)
    h, w = lum.shape
    out = {}
    for el in elements:
        b = el["box"]
        x0 = int(max(0, math.floor(b["x"] * dpr)))
        y0 = int(max(0, math.floor(b["y"] * dpr)))
        x1 = int(min(w, math.ceil((b["x"] + b["w"]) * dpr)))
        y1 = int(min(h, math.ceil((b["y"] + b["h"]) * dpr)))
        if x1 <= x0 or y1 <= y0:
            continue
        behind = float(np.percentile(lum[y0:y1, x0:x1], 95))
        colors = el.get("colors") or []
        for i, c in enumerate(colors):
            key = el["name"] if len(colors) == 1 else f"{el['name']}[{i}]"
            out[key] = round(contrast_from_luminance(relative_luminance(c), behind), 3)
    return {"elements": out, "min": (min(out.values()) if out else None)}


# ------------------------------------------------------------------ frames

def _load(path):
    return np.asarray(Image.open(path).convert("RGB"))


def judge_image(png_path, debug_dir=None):
    png_path = Path(png_path)
    m = FRAME_RE.match(png_path.name)
    side = png_path.with_suffix(".json")
    meta = json.loads(side.read_text()) if side.exists() else {}
    page = _load(png_path)
    # the capture's second frame with the DOM column hidden: creature + water
    # only, so the page's own white headings do not count as clipping, the
    # header text is out of the way of the rim and the strip, and it IS the
    # ruling's text-hidden frame for header_contrast
    bare_path = png_path.with_name(png_path.stem + "-bare.png")
    bare = _load(bare_path) if bare_path.exists() else None
    img = bare if bare is not None else page
    dpr = float(meta.get("dpr") or (m.group("dpr") if m else 1))
    css_w = int(m.group("w")) if m else int(round(img.shape[1] / dpr))
    out = {
        "variant": meta.get("variant") or (m.group("variant") if m else None),
        "size": meta.get("size") or (f"{m.group('w')}x{m.group('h')}@{m.group('dpr')}" if m else None),
        "engine": meta.get("engine"), "species": meta.get("species"),
        "tier": meta.get("tier") or (m.group("tier") if m else None),
        "px": [int(img.shape[1]), int(img.shape[0])],
        "measured_on": bare_path.name if bare is not None else png_path.name + " (page, no -bare frame)",
        "rim_crispness": None, "rim_peak": None, "rim_width_px": None,
        "detail_energy": None, "detail_energy_rect": None,
        "max_flat_run": None, "distinct_levels": None,
        "clipped_pct": round(clipped_pct(img), 4), "clipped_white_pct": round(clipped_white_pct(img), 4),
        "clipped_pct_page": round(clipped_pct(page), 4),
        "header_contrast": None, "header_contrast_elements": None,
        "frame_ms": meta.get("frame_ms"), "draw_calls_per_frame": meta.get("draw_calls_per_frame"),
        "frames_page": meta.get("frames"), "frames_bare": meta.get("frames_bare"),
        "errors": meta.get("errors", []),
    }
    hb = meta.get("heroBox")
    if hb:
        box = (hb["x"] * dpr, hb["y"] * dpr, hb["w"] * dpr, hb["h"] * dpr)
        dbg = (Path(debug_dir) / (png_path.stem + "-rim.png")) if debug_dir else None
        rm = rim_metrics(img, box, dbg)
        out["rim_crispness"] = round(rm["crispness"], 3)
        out["rim_peak"] = round(rm["peak"], 3)
        out["rim_width_px"] = round(rm["width_px"], 3)
        organs = crop_rects(hb, dpr, img.shape[1], img.shape[0])["organs"]
        out["detail_energy"] = round(detail_energy(img, organs), 3)
        out["detail_energy_rect"] = list(organs)
        out["heroBoxSource"] = meta.get("heroBoxSource")
    strip = meta.get("water_strip")
    if strip:
        wb = water_banding(img, (strip["x0"] * dpr, strip["x1"] * dpr))
        out["max_flat_run"] = wb["max_flat_run"]
        out["distinct_levels"] = wb["distinct_levels"]
    header = meta.get("header")
    if header and css_w <= PHONE_MAX_CSS_W:
        hc = header_contrast(img, header, dpr)
        out["header_contrast"] = hc["min"]
        out["header_contrast_elements"] = hc["elements"]
        out["header_contrast_source"] = bare_path.name if bare is not None else png_path.name + " (text visible!)"
    return out


def judge_dir(out_dir, debug_dir=None):
    out_dir = Path(out_dir)
    results = {}
    for png in sorted(out_dir.glob("*.png")):
        if not FRAME_RE.match(png.name):
            continue      # crops, *-bare.png, debug overlays (the -low probes DO match)
        results[png.name] = judge_image(png, debug_dir)
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out_dir", help="directory written by tools/jelly_capture.py")
    ap.add_argument("--debug-dir", help="write *-rim.png overlays of the silhouette band here")
    ap.add_argument("--quiet", action="store_true", help="write judge.json only; print nothing")
    args = ap.parse_args(argv)
    if args.debug_dir:
        Path(args.debug_dir).mkdir(parents=True, exist_ok=True)
    results = judge_dir(args.out_dir, args.debug_dir)
    text = json.dumps(results, indent=2, sort_keys=False)
    (Path(args.out_dir) / "judge.json").write_text(text + "\n")
    if not args.quiet:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
