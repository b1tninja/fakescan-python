"""The ``diag`` page layout v2: ground-truth layout, rendering and the per-element analyzer.

Offline (no server): needs the native module built with the diag bindings (``maturin develop``) and Pillow.
Injected distortions are applied to the rendered page with Pillow and the analyzer must recover them.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageFilter  # noqa: E402

from fakescan import diag  # noqa: E402

try:
    from fakescan import _native

    HAVE_NATIVE = hasattr(_native, "diag_layout_json")
except Exception:  # pragma: no cover
    HAVE_NATIVE = False

pytestmark = pytest.mark.skipif(not HAVE_NATIVE, reason="native module lacks the diag bindings; run maturin develop")

DPI = 100
WHITE = (255, 255, 255)


@pytest.fixture(scope="module")
def layout():
    return diag.diag_page_layout(DPI, version=2)


@pytest.fixture(scope="module")
def page():
    return diag.render_diag_page(DPI, version=2)


@pytest.fixture(scope="module")
def baseline(page, layout):
    return diag.analyze_diag_page(page, layout)


def analyze(img, layout, **kw):
    return diag.analyze_diag_page(img, layout, **kw)


def scaled(img, sx, sy):
    return img.resize((round(img.width * sx), round(img.height * sy)), Image.Resampling.BICUBIC)


# ------------------------------------------------------------------------------------------------- layout
def test_layout_is_deterministic_versioned_and_declares_srgb(layout):
    assert layout == diag.diag_page_layout(DPI, version=2)
    assert layout["layout_version"] == 2 and layout["colour_space"] == "sRGB"
    assert layout["box_mm"] == [206.0, 288.0]
    assert layout["qr_payload_layout_field"] == "lay=2"
    assert diag.diag_page_layout(DPI, version=1)["layout_version"] == 1
    a, b = diag.render_diag_png(DPI, 0, version=2), diag.render_diag_png(DPI, 0, version=2)
    assert a == b and b"sRGB" in a[:200]
    ids = [e["id"] for e in layout["elements"]]
    assert len(ids) == len(set(ids))
    for must in ("page_border", "printable_frame", "sheet_a4", "sheet_letter", "ruler_metric_h", "ruler_inch_h",
                 "ruler_metric_v", "ruler_inch_v", "measure_bar", "hue_wheel", "ramp_r", "siemens_star",
                 "sierpinski", "golden", "hexagon", "circle_in_square", "concentric_circles", "marker_0"):
        assert must in ids, must
    for e in layout["elements"]:
        x0, y0, x1, y1 = e["rect_mm"]
        assert x1 > x0 and y1 > y0 and e["size_mm"] == pytest.approx([x1 - x0, y1 - y0])
    els = diag.elements_by_id(layout)
    assert els["printable_frame"]["expected"]["inset_mm"]["left"] == 3.0
    assert els["sheet_a4"]["drawn"] is False  # does not fit the raster
    golden = els["golden"]["expected"]
    assert [s["size_units"] for s in golden["squares"]][:2] == [13.0, 8.0]
    assert golden["squares"][0]["size_mm"] == 26.0
    assert golden["phi"] == pytest.approx(1.618, abs=1e-3)
    r = els["ruler_metric_h"]["expected"]
    assert r["pitch_mm"] == 1.0 and r["ticks_mm"][:3] == [34.0, 35.0, 36.0] and r["axis"] == "x"
    assert els["ruler_inch_h"]["expected"]["pitch_mm"] == pytest.approx(25.4 / 16)


@pytest.mark.parametrize("dpi", [100, 150, 200])
def test_layout_samples_match_rendered_pixels(dpi):
    lay = diag.diag_page_layout(dpi, version=2)
    img = diag.render_diag_page(dpi, version=2)
    assert list(img.size) == lay["raster_px"]
    k = lay["px_per_mm"]
    n = 0
    for el in lay["elements"]:
        for s in el["samples"]:
            p = img.getpixel((int(s["x_mm"] * k), int(s["y_mm"] * k)))
            assert max(abs(p[i] - s["rgb"][i]) for i in range(3)) <= s["tol"], (dpi, el["id"], s, p)
            n += 1
    assert n > 200


def test_v2_only_adds_elements_in_declared_regions(layout):
    v1 = diag.render_diag_page(DPI, 0, version=1)
    v2 = diag.render_diag_page(DPI, 0, version=2)
    k = layout["px_per_mm"]
    regions = [e["rect_mm"] for e in layout["elements"] if e["since_layout"] == 2 and e["drawn"] and e["kind"] != "frame"]
    regions += [e["rect_mm"] for e in layout["elements"] if e["id"] == "printable_frame"]
    qr = (128.0, 206.0, 186.0, 266.0)
    wm, hm = layout["raster_mm"]
    changed = 0
    d1, d2 = v1.load(), v2.load()
    for y in range(v1.height):
        for x in range(v1.width):
            if d1[x, y] == d2[x, y]:
                continue
            changed += 1
            mx, my = (x + 0.5) / k, (y + 0.5) / k
            ok = any(r[0] - 0.4 <= mx <= r[2] + 0.6 and r[1] - 0.4 <= my <= r[3] + 0.6 for r in regions)
            ok = ok or (qr[0] <= mx <= qr[2] and qr[1] <= my <= qr[3])
            ok = ok or ((mx < 9.5 or mx > wm - 9.5) and (my < 9.5 or my > hm - 9.5))
            # the printable frame is one rectangle outline: only its four lines are drawn, not its interior
            assert ok, (x, y, mx, my)
    assert changed > 1000


def test_old_analyzer_results_do_not_change_on_the_v2_page():
    root = os.environ.get("PDFCRAFT_FAKESCAN")
    if not root:
        pytest.skip("set PDFCRAFT_FAKESCAN to a pdfcraft-fakescan checkout to compare with its analyzer")
    path = Path(root) / "pdfcraft_fakescan" / "pattern_analyzer.py"
    if not path.exists():
        pytest.skip("pdfcraft_fakescan.pattern_analyzer not available")
    spec = importlib.util.spec_from_file_location("pa_compat", path)
    pa = importlib.util.module_from_spec(spec)
    sys.modules["pa_compat"] = pa
    spec.loader.exec_module(pa)
    from dataclasses import asdict

    for dpi in (100, 200):
        f1 = asdict(pa.analyze(diag.render_diag_page(dpi, 0, version=1), dpi))
        f2 = asdict(pa.analyze(diag.render_diag_page(dpi, 0, version=2), dpi))
        for key, v1 in f1.items():
            v2 = f2[key]
            if key == "orientation_confidence":
                # whole-page profile score: the added ink lowers the margin a little (0.33 -> 0.30), never the verdict
                assert v2 >= 0.25 and abs(v2 - v1) < 0.06, (dpi, key, v1, v2)
            elif isinstance(v1, float):
                assert v2 == pytest.approx(v1, abs=5e-4), (dpi, key)
            elif isinstance(v1, (tuple, list)) and v1 and isinstance(v1[0], float):
                assert v2 == pytest.approx(list(v1), abs=0.02), (dpi, key)
            else:
                assert v2 == v1, (dpi, key, v1, v2)


# ------------------------------------------------------------------------------------------------- identity
def test_identity_round_trip_has_no_error(baseline, layout):
    s = baseline["summary"]
    g = baseline["geometry"]
    assert g["crosses_found"] == 4 and g["fit_residual_px_rms"] < 0.2
    assert s["scale_x"] == pytest.approx(1.0, abs=0.0015) and s["scale_y"] == pytest.approx(1.0, abs=0.0015)
    assert abs(s["rotation_deg"]) < 0.05 and abs(s["shear"]) < 0.002
    assert max(abs(v) for v in s["origin_offset_mm"]) < 0.25
    assert s["clipped_edges"] == []
    assert s["shadows_clipped_steps"] == 0 and s["highlights_clipped_steps"] == 0
    el = baseline["elements"]
    for edge in el["printable_frame"]["edges"].values():
        assert edge["found"] and abs(edge["margin_error_mm"]) < 0.3
    assert el["page_border"]["intact"]
    for rid in ("ruler_metric_h", "ruler_metric_v", "ruler_inch_h", "ruler_inch_v"):
        r = el[rid]
        assert r["ok"] and r["dpi"] == pytest.approx(DPI, rel=0.003), rid
        assert abs(r["length_error_pct"]) < 0.3
    assert abs(el["measure_bar"]["length_error_mm"]) < 0.15
    for rid in ("ramp_r", "ramp_g", "ramp_b", "ramp_n", "wedge_grey16", "patches_rgbcmy", "half_primaries", "skin_tones"):
        assert el[rid]["rgb_err_max"] <= 2.0 and el[rid]["delta_e_max"] < 2.0, rid
    assert el["hue_wheel"]["hue_err_deg_max_abs"] < 3.0
    assert abs(el["hue_wheel"]["hue_err_deg_mean_signed"]) < 1.0
    # resolution: coarse patches fully visible, the finest are not
    rel = el["res_lines_v"]["modulation_rel"]
    assert rel[0] > 0.8 and rel[-2] < 0.25
    assert el["res_lines_v"]["last_resolvable_period_mm"] in (0.5, 0.4)
    assert el["slant_edge_a"]["ok"] and 0.4 < el["slant_edge_a"]["rise_10_90_px"] < 2.5
    assert el["siemens_star"]["limit_lp_per_mm"] and el["siemens_star"]["limit_lp_per_mm"] > 2.0
    assert el["sierpinski"]["smallest_resolved_level"] >= 4
    # geometry
    gold = el["golden"]
    assert gold["ok"] and abs(gold["aspect_error_pct"]) < 0.3 and abs(gold["phi_error_pct"]) < 0.5
    assert el["concentric_circles"]["largest"]["aspect_error_pct"] < 0.5
    assert el["hexagon"]["aspect_error_pct"] < 0.8
    for i in range(4):
        assert abs(el[f"marker_{i}"]["error_deg"]) < 3.0
    assert el["text_12pt"]["legible"] and el["text_10pt"]["legible"]
    assert not [k for k, v in el.items() if "error" in v and isinstance(v["error"], str)]


# ------------------------------------------------------------------------------------------------- injected distortions
def test_recovers_anisotropic_scale_0_97_by_1_00(page, layout):
    rep = analyze(scaled(page, 0.97, 1.0), layout)
    s, el = rep["summary"], rep["elements"]
    assert s["scale_x"] == pytest.approx(0.97, abs=0.004) and s["scale_y"] == pytest.approx(1.0, abs=0.004)
    assert s["aspect_x_over_y"] == pytest.approx(0.97, abs=0.004)
    assert abs(s["rotation_deg"]) < 0.1
    assert el["golden"]["aspect_x_over_y"] == pytest.approx(0.97, abs=0.006)
    assert el["golden"]["width_over_height"] / el["golden"]["expected_width_over_height"] == pytest.approx(0.97, abs=0.008)
    circ = el["concentric_circles"]["largest"]
    assert circ["x_over_y"] == pytest.approx(0.97, abs=0.008) and circ["aspect_error_pct"] > 2.0
    assert el["hexagon"]["x_over_y"] == pytest.approx(0.97, abs=0.012)
    assert el["ruler_metric_h"]["dpi"] == pytest.approx(97.0, rel=0.004)
    assert el["ruler_metric_v"]["dpi"] == pytest.approx(100.0, rel=0.004)
    assert el["ruler_inch_h"]["dpi"] == pytest.approx(97.0, rel=0.004)
    bar = el["measure_bar"]
    assert bar["length_error_pct"] == pytest.approx(-3.0, abs=0.3) and bar["length_error_mm"] == pytest.approx(-5.7, abs=0.6)
    assert not rep["summary"]["clipped_edges"]


def test_recovers_a_3_percent_dpi_mismatch(page, layout):
    """Printed / scanned at 103 %: the rulers read 103 dpi, the 190 mm bar is 5.7 mm too long."""
    rep = analyze(scaled(page, 1.03, 1.03), layout)
    el = rep["elements"]
    for rid in ("ruler_metric_h", "ruler_metric_v", "ruler_inch_h", "ruler_inch_v"):
        assert el[rid]["dpi"] == pytest.approx(103.0, rel=0.004), rid
        assert el[rid]["scale_error_pct"] == pytest.approx(3.0, abs=0.4)
    assert el["measure_bar"]["length_error_mm"] == pytest.approx(5.7, abs=0.6)
    assert rep["summary"]["aspect_x_over_y"] == pytest.approx(1.0, abs=0.003)
    # the same raster judged against a scan_dpi of 103 shows no error
    ok = analyze(scaled(page, 1.03, 1.03), layout, scan_dpi=103.0)
    assert ok["summary"]["scale_x"] == pytest.approx(1.0, abs=0.004)
    assert abs(ok["elements"]["measure_bar"]["length_error_mm"]) < 0.4


def test_recovers_rotation_half_a_degree(page, layout):
    img = page.rotate(0.5, resample=Image.Resampling.BICUBIC, fillcolor=WHITE)  # counter-clockwise on screen
    rep = analyze(img, layout)
    assert rep["summary"]["rotation_deg"] == pytest.approx(-0.5, abs=0.06)
    assert abs(rep["summary"]["shear"]) < 0.003
    assert rep["summary"]["scale_x"] == pytest.approx(1.0, abs=0.004)
    ang = rep["elements"]["slant_edge_a"]["edge_angle_deg"]
    assert ang == pytest.approx(-5.0 - 0.5, abs=0.4) or ang == pytest.approx(-5.0 + 0.5, abs=0.4)
    for i in range(4):
        assert abs(rep["elements"][f"marker_{i}"]["error_deg"]) < 3.0


def test_recovers_shear_0_01(page, layout):
    img = page.transform(page.size, Image.Transform.AFFINE, (1, 0.01, 0, 0, 1, 0), resample=Image.Resampling.BICUBIC, fillcolor=WHITE)
    rep = analyze(img, layout)
    assert abs(rep["summary"]["shear"]) == pytest.approx(0.01, abs=0.0015)
    assert abs(rep["summary"]["rotation_deg"]) < 0.08
    assert rep["summary"]["scale_y"] == pytest.approx(1.0, abs=0.004)
    # right angles are no longer right: markers show about 0.57 degrees
    errs = [abs(rep["elements"][f"marker_{i}"]["error_deg"]) for i in range(4)]
    assert all(e < 4.0 for e in errs)


def test_margin_offset_and_clipping_are_reported(page, layout):
    shifted = Image.new("RGB", page.size, WHITE)
    shifted.paste(page, (24, 12))
    rep = analyze(shifted, layout)
    ox, oy = rep["summary"]["origin_offset_mm"]
    k = layout["px_per_mm"]
    assert ox == pytest.approx(24 / k, abs=0.25) and oy == pytest.approx(12 / k, abs=0.25)
    # the shifted sheet lost its right and bottom strip: those frame edges are no longer where they belong
    cropped = page.crop((20, 0, page.width, page.height))
    rep = analyze(cropped, layout)
    clipped = rep["summary"]["clipped_edges"]
    assert "page_border:left" in clipped and "printable_frame:left" in clipped
    assert not any(c.endswith(":right") or c.endswith(":bottom") for c in clipped)
    assert not rep["elements"]["page_border"]["edges"]["right"]["inset_px"]


def test_blur_lowers_the_resolution_figures(page, layout, baseline):
    blurred = page.filter(ImageFilter.GaussianBlur(1.6))
    rep = analyze(blurred, layout)
    b, a = rep["elements"], baseline["elements"]
    assert b["sierpinski"]["smallest_resolved_level"] < a["sierpinski"]["smallest_resolved_level"]
    assert (b["res_lines_v"]["last_resolvable_period_mm"] or 99) > a["res_lines_v"]["last_resolvable_period_mm"]
    assert b["siemens_star"]["limit_lp_per_mm"] < a["siemens_star"]["limit_lp_per_mm"]
    assert b["slant_edge_a"]["rise_10_90_px"] > a["slant_edge_a"]["rise_10_90_px"] + 1.0
    assert abs(rep["summary"]["scale_x"] - 1.0) < 0.003


def test_clipping_and_hue_errors_are_detected(page, layout):
    hi = page.point(lambda v: min(255, int(v * 1.3)))
    rep = analyze(hi, layout)
    assert rep["summary"]["highlights_clipped_steps"] >= 3
    assert rep["summary"]["highlights_clipped_up_to_level"] <= 247
    assert rep["elements"]["ramp_n"]["rgb_err_max"] > 20
    low = page.point(lambda v: max(0, int((v - 12) * 255 / 243)))
    assert analyze(low, layout)["summary"]["shadows_clipped_steps"] >= 3
    h, s, v = page.convert("HSV").split()
    shifted = Image.merge("HSV", (h.point(lambda x: (x + 8) % 256), s, v)).convert("RGB")
    rep = analyze(shifted, layout)
    assert rep["elements"]["hue_wheel"]["hue_err_deg_mean_signed"] == pytest.approx(8 * 360 / 256, abs=2.0)
    assert rep["elements"]["patches_rgbcmy"]["delta_e_max"] > 5


def test_report_is_json_serialisable(baseline):
    json.dumps(baseline)
    assert baseline["colour_space"] == "sRGB"
