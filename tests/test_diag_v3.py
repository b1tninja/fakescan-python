"""The ``diag`` page layout v3: parametric generator, layout JSON as ground truth, analyzer.

No stored page fixtures: every page, layout and QR payload is generated from parameters. Needs the native module
built with the diag bindings (``maturin develop``) and Pillow.
"""

from __future__ import annotations

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops  # noqa: E402

from fakescan import diag  # noqa: E402

try:
    from fakescan import _native

    HAVE_NATIVE = hasattr(_native, "diag_paper_sizes")
except Exception:  # pragma: no cover
    HAVE_NATIVE = False

pytestmark = pytest.mark.skipif(not HAVE_NATIVE, reason="native module lacks the v3 diag bindings; run maturin develop")

SIZES = [
    ("A4", 100, {}),
    ("A4", 150, {}),
    ("LETTER", 100, {}),
    ("A5", 150, {}),
    ("A3", 75, {}),
    ("LEGAL", 100, {}),
    ("A4", 100, {"orientation": "landscape"}),
    ("210x297", 100, {}),
    ("JIS-B5", 100, {}),
]


def pages(paper, dpi, extra, **more):
    kw = dict(paper=paper, **extra, **more)
    return diag.diag_page_layout(dpi, **kw), diag.render_diag_page(dpi, **kw)


def test_paper_table_and_determinism():
    sizes = diag.diag_paper_sizes()
    assert sizes["A4"] == (210.0, 297.0) and sizes["LETTER"] == (215.9, 279.4)
    a = diag.render_diag_png(100, paper="A4", page=1, total=3, job="t")
    assert a == diag.render_diag_png(100, paper="A4", page=1, total=3, job="t")
    assert a[:8] == b"\x89PNG\r\n\x1a\n" and b"sRGB" in a[:200] and b"pHYs" in a[:200]
    assert diag.diag_page_layout(100, paper="A4") == diag.diag_page_layout(100, paper="A4")


@pytest.mark.parametrize("paper,dpi,extra", SIZES)
def test_layout_samples_match_the_raster_at_any_size(paper, dpi, extra):
    lay, img = pages(paper, dpi, extra, page=3, total=9, side="back", job="job7")
    assert lay["layout_version"] == 3 and lay["colour_space"] == "sRGB"
    k = lay["px_per_mm"]
    assert img.size == tuple(lay["raster_px"])
    checked = 0
    for el in lay["elements"]:
        if not el["drawn"]:
            continue
        for s in el["samples"]:
            px = img.getpixel((int(s["x_mm"] * k), int(s["y_mm"] * k)))
            assert max(abs(px[i] - s["rgb"][i]) for i in range(3)) <= s["tol"], (el["id"], s, px)
            checked += 1
    assert checked > 100


@pytest.mark.parametrize("paper,dpi,extra", SIZES)
def test_identity_analysis_at_any_size(paper, dpi, extra):
    lay, img = pages(paper, dpi, extra, page=1, side="front")
    rep = diag.analyze_diag_page(img, lay)
    s = rep["summary"]
    assert abs(s["scale_x_vs_nominal"] - 1) < 0.005 and abs(s["scale_y_vs_nominal"] - 1) < 0.005
    assert abs(s["rotation_deg"]) < 0.05
    assert s["orientation"] == "upright" and s["channel_order"] == "rgb"
    assert s["colorchecker_mean_delta_e76"] < 1.0
    assert s["order_bars_ok"] and s["page_number_ok"] and s["side_marker_ok"]
    assert s["clipped_edges"] == []
    assert abs(s["measure_bar_length_error_mm"]) < 0.4
    assert abs(s["ruler_metric_pitch_error_pct"]) < 0.3


def test_scale_rotation_and_channel_faults_are_recovered():
    lay, img = pages("A4", 100, {})
    sx = diag.analyze_diag_page(img.resize((round(img.width * 0.97), img.height), Image.Resampling.BICUBIC), lay)
    assert abs(sx["summary"]["scale_x_vs_nominal"] - 0.97) < 0.01
    assert abs(sx["summary"]["scale_y_vs_nominal"] - 1.0) < 0.01
    assert abs(sx["summary"]["aspect_x_over_y"] - 0.97) < 0.01
    rot = diag.analyze_diag_page(img.rotate(-0.5, resample=Image.Resampling.BICUBIC, fillcolor=(255, 255, 255)), lay)
    assert abs(rot["summary"]["rotation_deg"] - 0.5) < 0.1
    r, g, b = img.split()
    bgr = diag.analyze_diag_page(Image.merge("RGB", (b, g, r)), lay)
    assert bgr["summary"]["channel_order"] == "bgr"
    assert bgr["summary"]["colorchecker_mean_delta_e76"] > 5


def test_orientation_and_duplex_markers():
    lay, img = pages("A4", 100, {}, side="front")
    assert diag.analyze_diag_page(img.rotate(180), lay)["summary"]["orientation"] == "rot180"
    assert diag.analyze_diag_page(img.transpose(Image.Transpose.FLIP_LEFT_RIGHT), lay)["summary"]["orientation"] == "flip_h"
    assert diag.analyze_diag_page(img.transpose(Image.Transpose.FLIP_TOP_BOTTOM), lay)["summary"]["orientation"] == "flip_v"
    layb, back = pages("A4", 100, {}, side="back")
    repb = diag.analyze_diag_page(back, layb)
    assert repb["summary"]["side_read"] == "back" and repb["summary"]["orientation"] == "upright"
    glyphs = lambda l: [e["expected"]["glyph"] for e in l["elements"] if e["id"].startswith("orient_")]  # noqa: E731
    assert glyphs(lay) != glyphs(layb)
    assert ImageChops.difference(img, back).getbbox() is not None


def test_page_order_survives_in_the_default_page():
    for page in (0, 6, 11):
        lay, img = pages("A4", 100, {}, page=page, total=12)
        o = diag.analyze_diag_page(img, lay)["elements"]["page_order"]
        assert o["page_number"]["read"] == str(page + 1)
        assert o["order_bars"]["filled_found"] == min(page + 1, 16)


def test_qr_payload_regenerates_the_page_and_primitives_are_complete():
    lay = diag.diag_page_layout(150, paper="LEGAL", page=2, copy=1, total=4, side="front", job="ab-1", margin_mm=6)
    spec = diag.diag_spec_from_qr(lay["qr_payload"])
    assert spec["paper"] == "LEGAL" and spec["page"] == 2 and spec["copy"] == 1 and spec["total"] == 4
    keys = ("paper", "width_px", "height_px", "margin_mm", "bleed_mm", "page", "copy", "total", "side", "job", "banner_px")
    again = diag.diag_page_layout(spec["dpi"], **{k: v for k, v in spec.items() if k in keys})
    assert again["elements"] == lay["elements"] and again["primitives"] == lay["primitives"]
    prims = diag.diag_page_primitives(lay)
    ops = {p["op"] for p in prims}
    assert {"rect", "text", "line", "circle", "ticks", "ramp", "qr", "polygon"} <= ops
    assert all(isinstance(p["vector"], bool) and "id" in p for p in prims)


def test_small_sheets_record_dropped_elements_and_keep_the_essentials():
    lay = diag.diag_page_layout(100, paper="A6")
    drawn = {e["id"] for e in lay["elements"] if e["drawn"]}
    assert {"qr", "printable_frame", "orient_tl", "reg_tl", "page_number"} <= drawn
    dropped = [e for e in lay["elements"] if not e["drawn"] and e["group"] == "dropped"]
    assert all(e["reason"] for e in dropped)
    assert diag.diag_page_layout(100, paper="A3")["scale"] > lay["scale"]


def test_gray_and_bleed_parameters():
    lay, img = pages("A4", 100, {}, colour="gray", bleed_mm=3)
    assert all(img.getpixel((x, y))[0] == img.getpixel((x, y))[1] for x in range(0, img.width, 97) for y in range(0, img.height, 89))
    assert any(e["id"] == "bleed_probes" for e in lay["elements"])
    assert lay["bleed_box_mm"][0] == -3.0


def test_v1_and_v2_still_selectable():
    assert diag.diag_page_layout(100, version=2)["layout_version"] == 2
    assert diag.diag_page_layout(100, version=1)["layout_version"] == 1
    assert diag.render_diag_page(100, version=2).size == (816, 1144)
