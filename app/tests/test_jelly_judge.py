"""Unit tests for the bake-off judge (tools/jelly_judge.py) on synthetic images.

numpy and Pillow are not in app/requirements.txt (CI installs only those plus
pytest), so this file skips itself where they are missing, like the browser
tests do without playwright.
"""

import importlib.util
import json
import pathlib

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

TOOLS = pathlib.Path(__file__).resolve().parents[2] / "tools"
spec = importlib.util.spec_from_file_location("jelly_judge", TOOLS / "jelly_judge.py")
jj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jj)


# ---- the brief's tests ----

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


# ---- the rest of the module ----

def test_max_flat_run_counts_equal_neighbours():
    assert jj.max_flat_run(np.array([1, 1, 1, 2, 2, 3], np.uint8)) == 3
    assert jj.max_flat_run(np.array([5], np.uint8)) == 1
    assert jj.max_flat_run(np.array([], np.uint8)) == 0
    # a channel-sum column (ints above 255) works the same
    assert jj.max_flat_run(np.array([700, 700, 701, 701, 701, 701], int)) == 4


def test_distinct_levels():
    assert jj.distinct_levels(np.array([1, 1, 2, 2, 9], np.uint8)) == 3


def test_clipped_pct_counts_any_channel_and_white_separately():
    img = np.zeros((10, 10, 3), np.uint8)
    img[0, :, 0] = 255             # a red-clipped row: clipped, not white
    img[1, :, :] = 255             # a white row
    assert jj.clipped_pct(img) == 20.0
    assert jj.clipped_white_pct(img) == 10.0


def test_relative_luminance_is_wcag():
    assert jj.relative_luminance((0, 0, 0)) == 0.0
    assert abs(jj.relative_luminance((255, 255, 255)) - 1.0) < 1e-9
    assert abs(jj.relative_luminance((255, 0, 0)) - 0.2126) < 1e-9
    # the low-end linear segment (sRGB <= 0.03928 -> /12.92)
    assert abs(jj.relative_luminance((10, 10, 10)) - (10 / 255 / 12.92)) < 1e-9


def test_contrast_ratio_is_symmetric_and_at_least_one():
    assert jj.contrast_ratio((0, 0, 0), (255, 255, 255)) == jj.contrast_ratio((255, 255, 255), (0, 0, 0))
    assert jj.contrast_ratio((40, 40, 40), (40, 40, 40)) == 1.0


def _disc(size=200, r=60, blur=0):
    """A grey disc (value 180) on dark water (20), optionally box-blurred."""
    yy, xx = np.mgrid[:size, :size]
    d = np.hypot(xx - size / 2, yy - size / 2)
    img = np.where(d <= r, 180.0, 20.0)
    for _ in range(blur):
        p = np.pad(img, 1, mode="edge")
        img = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + p[1:-1, 1:-1]) / 5
    return np.repeat(img[:, :, None], 3, axis=2).astype(np.uint8)


BOX = (100 - 66, 100 - 66, 132, 132)     # the heroBox: a little larger than the disc


def test_rim_crispness_drops_when_the_edge_spreads_past_the_band():
    """The brief's band mean: a step that fits in the 6 px band scores its full
    contrast; one blurred to sigma ~6 px leaves most of it outside."""
    sharp = jj.rim_crispness(_disc(), BOX)
    soft = jj.rim_crispness(_disc(blur=100), BOX)
    assert sharp > 2 * soft > 0


def test_rim_sharpness_separates_a_mild_blur_the_band_mean_cannot():
    """A ~3 px blur keeps nearly the same band mean (same contrast) but halves
    the peak and widens the transition: that is what rim_peak / rim_width_px
    are for."""
    sharp = jj.rim_metrics(_disc(), BOX)
    soft = jj.rim_metrics(_disc(blur=6), BOX)
    assert soft["crispness"] > 0.8 * sharp["crispness"]
    assert sharp["peak"] > 1.8 * soft["peak"]
    assert sharp["width_px"] <= 2.5 < soft["width_px"]


def test_rim_silhouette_lands_on_the_edge():
    """The ring the judge picks is the disc's edge, whatever the box's aspect
    or offset (the heroBox overstates the apex by ~14% of the scale), and it
    is the UPPER arc: the exumbrella against open water."""
    box = (100 - 70, 100 - 80, 140, 150)
    pts = jj.rim_silhouette(jj.luminance(_disc()), box)
    d = np.hypot(pts[:, 0] - 100, pts[:, 1] - 100)
    assert abs(np.median(d) - 60) <= 1.5
    assert np.percentile(np.abs(d - 60), 90) <= 3
    assert pts[:, 1].min() < 45 and np.percentile(pts[:, 1], 90) < 125   # over the top, not under


def test_rim_silhouette_drops_rays_that_leave_the_frame():
    """A bell cut by the frame edge: rays that find only water (peak far below
    the ring's median) are dropped instead of pinning the band to noise."""
    img = _disc(size=200)[:, 60:]          # the disc's left third is off-frame
    pts = jj.rim_silhouette(jj.luminance(img), (100 - 66 - 60, 100 - 66, 132, 132))
    assert (pts[:, 0] >= 0).all()
    d = np.hypot(pts[:, 0] - 40, pts[:, 1] - 100)
    assert np.percentile(np.abs(d - 60), 90) <= 3


def test_rim_crispness_is_the_band_mean_not_the_box_mean():
    """A sharp edge inside a box full of flat water: the metric measures the
    6 px band, so padding the box with more water must not dilute it."""
    a = jj.rim_crispness(_disc(size=300), (150 - 66, 150 - 66, 132, 132))
    b = jj.rim_crispness(_disc(size=300), (150 - 100, 150 - 100, 200, 200))
    assert abs(a - b) / a < 0.15


def test_sobel_magnitude_on_a_step():
    img = np.zeros((10, 10), float); img[:, 5:] = 100
    mag = jj.sobel_magnitude(img)
    assert mag[5, 4] > 100 and mag[5, 5] > 100      # the step's two columns
    assert mag[5, 1] == 0 and mag[5, 8] == 0        # flat either side


def test_water_banding_samples_the_strip():
    img = np.zeros((400, 300, 3), np.uint8)
    img[:, :, :] = np.linspace(10, 60, 400)[:, None, None].astype(np.uint8)   # 8-px steps
    res = jj.water_banding(img, (0, 100))
    assert res["max_flat_run"] >= 8
    assert 0 <= min(res["columns"]) and max(res["columns"]) < 100
    assert res["distinct_levels"] > 20


def test_header_contrast_uses_the_95th_percentile_behind_each_element():
    frame = np.zeros((100, 100, 3), np.uint8)
    frame[:, :, :] = 10
    frame[40:45, :, :] = 200                # a bright creature band under the eyebrow
    elements = [
        {"name": "wordmark", "colors": [[255, 255, 255], [118, 118, 118]], "box": {"x": 0, "y": 0, "w": 50, "h": 10}},
        {"name": "eyebrow", "colors": [[255, 255, 255]], "box": {"x": 0, "y": 20, "w": 50, "h": 10}},
    ]
    res = jj.header_contrast(frame, elements, dpr=2)     # CSS boxes x2: the eyebrow spans rows 40-60
    assert set(res["elements"]) == {"wordmark[0]", "wordmark[1]", "eyebrow"}
    assert res["elements"]["wordmark[0]"] > 15
    assert res["elements"]["eyebrow"] < 2            # the 95th percentile finds the bright band
    assert res["min"] == min(res["elements"].values())


def test_judge_image_reads_the_sidecar(tmp_path):
    img = _disc(size=400)
    Image.fromarray(img).save(tmp_path / "moon-1440x900@1.png")
    (tmp_path / "moon-1440x900@1.json").write_text(json.dumps({
        "variant": "moon", "size": "1440x900@1", "dpr": 1, "tier": "ultra", "engine": "hd",
        "heroBox": {"x": 200 - 66, "y": 200 - 66, "w": 132, "h": 132}, "water_strip": {"x0": 0, "x1": 100},
        "frame_ms": 333.3, "draw_calls_per_frame": 12, "errors": []}))
    out = jj.judge_image(tmp_path / "moon-1440x900@1.png")
    assert out["rim_crispness"] > 0 and out["rim_peak"] > 0 and out["rim_width_px"] > 0
    assert out["max_flat_run"] >= 1
    assert out["clipped_pct"] == 0.0
    assert out["header_contrast"] is None          # not a phone frame
    assert out["frame_ms"] == 333.3 and out["draw_calls_per_frame"] == 12
    assert out["tier"] == "ultra"


def test_judge_dir_skips_crops_and_bare_frames(tmp_path):
    img = _disc(size=100)
    for name in ("moon-390x844@3.png", "moon-390x844@3-bare.png", "moon-crop-rim.png"):
        Image.fromarray(img).save(tmp_path / name)
    out = jj.judge_dir(tmp_path)
    assert list(out) == ["moon-390x844@3.png"]
    assert out["moon-390x844@3.png"]["rim_crispness"] is None      # no sidecar -> no box
    assert out["moon-390x844@3.png"]["measured_on"] == "moon-390x844@3-bare.png"


def test_judge_measures_the_bare_frame_and_reports_page_clipping(tmp_path):
    """The page frame carries the DOM's white headings; the bare frame (DOM
    column hidden) is what the pixel metrics read, and header_contrast reads
    the creature behind the header from it too."""
    bare = np.full((100, 100, 3), 20, np.uint8)               # dark water
    page = bare.copy(); page[0:5, :, :] = 255                 # a white heading inside the header box
    Image.fromarray(page).save(tmp_path / "moon-390x844@3.png")
    Image.fromarray(bare).save(tmp_path / "moon-390x844@3-bare.png")
    (tmp_path / "moon-390x844@3.json").write_text(json.dumps({
        "variant": "moon", "size": "390x844@3", "dpr": 1, "tier": "high", "engine": "hd",
        "header": [{"name": "eyebrow", "colors": [[255, 255, 255]], "box": {"x": 0, "y": 0, "w": 100, "h": 10}}]}))
    out = jj.judge_image(tmp_path / "moon-390x844@3.png")
    assert out["clipped_pct"] == 0.0 and out["clipped_pct_page"] == 5.0
    assert out["header_contrast_source"] == "moon-390x844@3-bare.png"
    assert out["header_contrast"] > 10          # the dark water behind, not the white heading (1.0)


# ---- fix round 1: frame indices, detail_energy, the Low-tier probe ----

def _checker(size=120, cell=6, blur=0):
    yy, xx = np.mgrid[:size, :size]
    img = np.where(((xx // cell) + (yy // cell)) % 2 == 0, 160.0, 40.0)
    for _ in range(blur):
        p = np.pad(img, 1, mode="edge")
        img = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] + p[1:-1, 1:-1]) / 5
    return np.repeat(img[:, :, None], 3, axis=2).astype(np.uint8)


def test_detail_energy_prefers_texture_over_its_blurred_copy():
    """Variance of the 3x3 Laplacian on luminance, per pixel: a sharp checker
    scores far above the same checker blurred, and a flat field scores ~0."""
    rect = (10, 10, 110, 110)
    sharp = jj.detail_energy(_checker(), rect)
    soft = jj.detail_energy(_checker(blur=4), rect)
    flat = jj.detail_energy(np.full((120, 120, 3), 90, np.uint8), rect)
    assert sharp > 4 * soft > 0
    assert flat == 0.0


def test_detail_energy_is_per_pixel_not_per_region():
    """The same texture in a bigger region gives the same number."""
    a = jj.detail_energy(_checker(size=120), (10, 10, 110, 110))
    b = jj.detail_energy(_checker(size=240), (10, 10, 230, 230))
    assert abs(a - b) / a < 0.05


def test_laplacian_of_a_ramp_is_zero_inside():
    img = np.tile(np.arange(20, dtype=float)[None, :], (10, 1))
    lap = jj.laplacian(img)
    assert np.abs(lap[1:-1, 1:-1]).max() < 1e-9


def test_crop_rects_are_framed_from_the_box_and_clamped():
    hb = {"x": 100, "y": 50, "w": 200, "h": 100}
    r = jj.crop_rects(hb, 2, 1000, 400)          # dpr 2 -> box (200, 100, 400, 200)
    assert set(r) == {"rim", "organs", "filaments"}
    x0, y0, x1, y1 = r["organs"]
    assert 200 < x0 < 600 and 100 < y0 < 300 and x1 <= 600 and y1 <= 300     # inside the box
    assert r["rim"][0] < 200 and r["rim"][2] > 600                            # the rim spans wider than the box
    assert r["filaments"][3] == 400                                           # clamped to the frame
    assert r["filaments"][1] >= r["organs"][1]                                # the drape starts below the interior


def test_judge_reports_both_frame_indices_and_detail_energy(tmp_path):
    page = _disc(size=400)
    Image.fromarray(page).save(tmp_path / "moon-1440x900@1.png")
    Image.fromarray(_checker(size=400, cell=8)).save(tmp_path / "moon-1440x900@1-bare.png")
    (tmp_path / "moon-1440x900@1.json").write_text(json.dumps({
        "variant": "moon", "size": "1440x900@1", "dpr": 1, "tier": "ultra", "engine": "hd",
        "heroBox": {"x": 200 - 66, "y": 200 - 66, "w": 132, "h": 132},
        "frames": 34, "frames_bare": 41}))
    out = jj.judge_image(tmp_path / "moon-1440x900@1.png")
    assert out["frames_page"] == 34 and out["frames_bare"] == 41
    assert "frames" not in out
    assert out["detail_energy"] > 100                # the checker is the measured (bare) frame
    assert out["detail_energy_rect"] == list(jj.crop_rects(
        {"x": 134, "y": 134, "w": 132, "h": 132}, 1, 400, 400)["organs"])


def test_judge_dir_includes_the_low_tier_probe(tmp_path):
    img = _disc(size=100)
    for name in ("moon-1440x900@1.png", "moon-1440x900@1-low.png", "moon-1440x900@1-low-bare.png"):
        Image.fromarray(img).save(tmp_path / name)
    (tmp_path / "moon-1440x900@1-low.json").write_text(json.dumps({
        "variant": "moon", "size": "1440x900@1", "tier": "low", "dpr": 1, "frame_ms": 99.0}))
    out = jj.judge_dir(tmp_path)
    assert list(out) == ["moon-1440x900@1-low.png", "moon-1440x900@1.png"]
    probe = out["moon-1440x900@1-low.png"]
    assert probe["tier"] == "low" and probe["frame_ms"] == 99.0
    assert probe["measured_on"] == "moon-1440x900@1-low-bare.png"
