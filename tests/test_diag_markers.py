"""ArUco markers, calibration boards and dual-unit rulers of the ``diag`` page (layout 3, revision 1).

Everything is generated from parameters (no stored pages).  The pure-Python detector is always tested; the ``cv2``
tests run only where ``opencv-contrib`` is importable and skip cleanly otherwise.
"""

from __future__ import annotations

import math

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from fakescan import diag  # noqa: E402
from fakescan import diag_markers as dm  # noqa: E402

try:
    from fakescan import _native

    HAVE_NATIVE = hasattr(_native, "diag_paper_sizes")
except Exception:  # pragma: no cover
    HAVE_NATIVE = False

pytestmark = pytest.mark.skipif(not HAVE_NATIVE, reason="native module lacks the v3 diag bindings")

PAPERS = [
    ("A4", 100, {}),
    ("LETTER", 100, {}),
    ("A5", 150, {}),
    ("A3", 75, {}),
    ("LEGAL", 100, {}),
    ("A4", 100, {"orientation": "landscape"}),
    ("custom", 100, {"width_mm": 190.0, "height_mm": 260.0}),
]


def pages(paper, dpi, extra, **more):
    kw = dict(extra, **more)
    if paper != "custom":
        kw["paper"] = paper
    return diag.diag_page_layout(dpi, **kw), diag.render_diag_page(dpi, **kw)


def transform(img, rot=0.0, scale=1.0, crop=0, flip=None):
    w, h = img.size
    out = img
    if flip == "h":
        out = out.transpose(Image.FLIP_LEFT_RIGHT)
    if flip == "v":
        out = out.transpose(Image.FLIP_TOP_BOTTOM)
    if rot or scale != 1.0:
        a = math.radians(rot)
        cs, sn = math.cos(a) / scale, math.sin(a) / scale
        cx, cy = w / 2, h / 2
        coef = (cs, sn, cx - cs * cx - sn * cy, -sn, cs, cy + sn * cx - cs * cy)
        out = out.transform((w, h), Image.AFFINE, coef, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    if crop:
        out = out.crop((crop, crop, w - crop, h - crop))
    return out


def markers_of(lay):
    return [e for e in lay["elements"] if e["kind"] == "marker" and e["drawn"]]


# ------------------------------------------------------------------------------------------------ dictionary
def test_dictionary_table_is_the_documented_one():
    assert dm.ARUCO_DICT == "DICT_4X4_50" and len(dm.ARUCO_4X4_50) == 20
    assert dm.ARUCO_4X4_50[0] == int.from_bytes(bytes([181, 50]), "big")
    assert dm.ARUCO_4X4_50[1] == int.from_bytes(bytes([15, 154]), "big")
    assert len(set(dm.ARUCO_4X4_50)) == 20
    for i in range(20):
        g = dm.aruco_cells(i)
        assert all(g[0][k] == g[5][k] == g[k][0] == g[k][5] == 1 for k in range(6))


def test_dictionary_matches_cv2_where_available():
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "aruco"):
        pytest.skip("cv2 without the aruco module")
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for i in range(20):
        img = d.generateImageMarker(i, 6) > 127
        assert [[0 if img[r][c] else 1 for c in range(6)] for r in range(6)] == dm.aruco_cells(i), i


def test_decoder_reads_every_symmetry():
    for mid in range(20):
        for mirrored, k, grid in dm._variants(dm.aruco_cells(mid)):
            dec = dm.decode_grid(grid)
            assert dec["id"] == mid and dec["score"] == 1.0
            # a marker equal to one of its own mirrors / rotations reads as the first equivalent symmetry
            same = [g for m_, k_, g in dm._variants(dm.aruco_cells(mid)) if (m_, k_ * 90) == (dec["mirrored"], dec["rotation_deg"])]
            assert same and same[0] == grid


# ------------------------------------------------------------------------------------------------ generation
@pytest.mark.parametrize("paper,dpi,extra", PAPERS)
def test_layout_describes_markers_boards_and_rulers_on_any_sheet(paper, dpi, extra):
    lay, img = pages(paper, dpi, extra)
    assert lay["layout_version"] == 3 and lay["layout_revision"] == 1
    assert img.size == tuple(lay["raster_px"])
    ms = markers_of(lay)
    ids = [e["expected"]["marker_id"] for e in ms]
    assert len(ids) == len(set(ids)) and {0, 1, 2, 3} <= set(ids)
    for e in ms:
        x = e["expected"]
        r = e["rect_mm"]
        assert x["dictionary"] == "DICT_4X4_50"
        side = r[2] - r[0]
        assert abs(side - 6 * x["module_mm"]) < 1e-9 and x["module_mm"] >= 0.6
        assert x["quiet_modules"] == 1 and x["quiet_zone_mm"] >= x["module_mm"] - 1e-9
        c = x["corners_mm"]
        assert c == [[r[0], r[1]], [r[2], r[1]], [r[2], r[3]], [r[0], r[3]]]  # TL, TR, BR, BL
        assert x["cells"] == ["".join(str(v) for v in row) for row in dm.aruco_cells(x["marker_id"])]
    # dropped fiducials say why
    for e in lay["elements"]:
        if not e["drawn"]:
            assert e.get("reason"), e["id"]
    # every marker / board / ruler / bracket is vector geometry (rect, circle, polygon, line, text, ticks)
    prims = diag.diag_page_primitives(lay)
    for p in prims:
        pid = p["id"]
        if pid.startswith(("marker_", "charuco", "bracket_", "ruler", "chessboard", "circle_grid")):
            assert p["vector"] is True and p["op"] in ("rect", "circle", "polygon", "line", "text", "ticks"), p
    # rulers: both scales per drawn ruler share the zero line and sit on opposite edges
    byid = {e["id"]: e for e in lay["elements"]}
    for m, i in (("ruler_metric", "ruler_inch"), ("ruler_v_metric", "ruler_v_inch")):
        if byid[m]["drawn"]:
            xm, xi = byid[m]["expected"], byid[i]["expected"]
            assert xm["zero_line_mm"] == xi["zero_line_mm"] and xm["tick_edge_mm"] != xi["tick_edge_mm"]
            assert xm["tiers"] == [[1, 1.5], [5, 2.5], [10, 3.5]]
            assert xi["tiers"] == [[1, 1.2], [2, 1.8], [4, 2.4], [8, 3.0], [16, 3.6]]
            assert len(xm["ticks"]["pos_mm"]) == len(xm["ticks"]["length_mm"]) > 20


def test_big_sheets_carry_all_eight_edge_markers_and_the_boards():
    for paper in ("A4", "LETTER", "A3", "LEGAL"):
        lay = diag.diag_page_layout(100, paper=paper)
        ids = sorted(e["expected"]["marker_id"] for e in markers_of(lay))
        assert ids == list(range(20)), paper
        byid = {e["id"]: e for e in lay["elements"]}
        assert byid["chessboard"]["expected"]["inner_corners"] == [8, 5]
        assert byid["charuco_board"]["expected"]["marker_ids"] == list(range(8, 20))
        cg = byid["circle_grid"]["expected"]
        assert cg["points"] == 30 and cg["pattern"] == "asymmetric" and len(cg["centres_mm"]) == 30
        circles = [p for p in diag.diag_page_primitives(lay) if p["id"] == "circle_grid" and p["op"] == "circle"]
        assert len(circles) == 30


def test_rulers_parameter_and_qr_scheme():
    for r, metric, inch in (("both", True, True), ("metric", True, False), ("inch", False, True)):
        lay = diag.diag_page_layout(100, paper="A4", rulers=r)
        assert lay["spec"]["rulers"] == r
        byid = {e["id"]: e for e in lay["elements"]}
        assert byid["ruler_metric"]["drawn"] is metric and byid["ruler_inch"]["drawn"] is inch
        assert byid["ruler_v_metric"]["drawn"] is metric and byid["ruler_v_inch"]["drawn"] is inch
        spec = diag.diag_spec_from_qr(lay["qr_payload"])
        assert spec["rulers"] == r and spec["layout_revision"] == 1
        assert f"|rl={r}|" in lay["qr_payload"] and "|ar=4x4_50|" in lay["qr_payload"] and "|id=0-19" in lay["qr_payload"]
    assert diag.diag_page_layout(100, paper="A4")["spec"]["rulers"] == "both"
    # a payload written before the revision keys still decodes (revision 0, both rulers)
    old = "fakescan|v1|page=2|pat=diag|side=front|lay=3|paper=A4|px=816x1144|dpi=100|m=4|b=0|copy=1|of=4|bn=44|g=0|job=AB-1"
    spec = diag.diag_spec_from_qr(old)
    assert spec["layout_revision"] == 0 and spec["rulers"] == "both" and spec["page"] == 2


def test_determinism_with_the_new_elements():
    a = diag.render_diag_png(100, paper="A4", page=1, total=3, job="t")
    assert a == diag.render_diag_png(100, paper="A4", page=1, total=3, job="t")
    assert diag.diag_page_layout(150, paper="LETTER") == diag.diag_page_layout(150, paper="LETTER")


# ------------------------------------------------------------------------------------------------ pure-Python detector
@pytest.mark.parametrize("paper,dpi", [("A4", 100), ("A4", 150), ("A4", 200), ("A4", 300), ("LETTER", 100), ("A3", 75), ("A5", 150)])
def test_python_detector_finds_every_marker_with_subpixel_corners(paper, dpi):
    lay, img = pages(paper, dpi, {})
    rep = diag.analyze_diag_page(img, lay, detector="python")
    m = rep["markers"]
    assert m["detector"] == "python"
    assert m["n_found"] == m["n_expected"] >= 6 and m["missing"] == []
    px_mm = 25.4 / dpi  # sub-pixel: well inside one pixel
    assert m["rms_corner_err_mm"] < 0.3 * px_mm + 0.03 and m["max_corner_err_mm"] < 0.6 * px_mm + 0.06
    assert m["orientation"]["verdict"] == "upright"
    assert m["homography"]["residual_rms_mm"] < 0.3 * px_mm + 0.03
    assert rep["summary"]["fit"] == "crosses" and rep["geometry"]["marker_check"]["agree"]
    assert not m["affine"]["mirrored"]


@pytest.mark.parametrize(
    "name,kw",
    [
        ("rotation", dict(rot=1.5)),
        ("scale", dict(scale=0.97)),
        ("crop", dict(crop=12)),
        ("rot_scale_crop", dict(rot=-1.0, scale=1.02, crop=8)),
    ],
)
def test_python_detector_after_rotation_scale_crop(name, kw):
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(transform(img, **kw), lay, detector="python")
    s = rep["summary"]
    assert s["fit"] == "crosses", s
    assert rep["markers"]["n_found"] == rep["markers"]["n_expected"]
    assert rep["markers"]["max_corner_err_mm"] < 0.2
    if "rot" in kw:
        assert abs(s["rotation_deg"] - kw["rot"]) < 0.05
    if "scale" in kw:
        assert abs(s["scale_x_vs_nominal"] - kw["scale"]) < 0.003


@pytest.mark.parametrize("flip,verdict", [("h", "flip_h"), ("v", "flip_v")])
def test_flips_are_named_by_the_markers_and_the_glyphs(flip, verdict):
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(transform(img, flip=flip), lay, detector="python")
    assert rep["summary"]["marker_orientation"] == verdict
    assert rep["summary"]["orientation"] == verdict
    # the corner markers decode as the markers of the opposite corners (or the same id mirrored)
    assert any(r.get("decoded_id") != r.get("marker_id") or r.get("mirrored") for r in rep["markers"]["per_marker"].values() if r["found"])


def test_rot180_is_reported():
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(img.rotate(180), lay, detector="python")
    assert rep["summary"]["orientation"] == "rot180"
    # every marker decodes as another id (the opposite position's) and the page is still named from them
    assert rep["summary"]["marker_orientation"] == "rot180"
    assert rep["markers"]["orientation"]["votes"] >= 6


# ------------------------------------------------------------------------------------------------ rulers
def test_ruler_dpi_per_axis_and_unit_and_baseline_error():
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(img, lay, detector="python")
    r = rep["rulers"]
    for axis in ("x", "y"):
        for unit in ("mm", "in"):
            v = r[axis][unit]
            assert abs(v["dpi"] - 100.0) < 0.1 and abs(v["length_error_mm"]) < 0.1
    assert r["y"]["mm"]["baseline_mm"] >= 200 and r["x"]["mm"]["baseline_mm"] >= 100
    # an anisotropic scale shows as different dpi on the two axes, with the same value for both units on an axis
    scaled = img.resize((int(img.width * 0.98), img.height), Image.BICUBIC)
    rep = diag.analyze_diag_page(scaled, lay, detector="python")
    r = rep["rulers"]
    assert abs(r["x"]["mm"]["dpi"] - 98.0) < 0.3 and abs(r["x"]["in"]["dpi"] - 98.0) < 0.3
    assert abs(r["y"]["mm"]["dpi"] - 100.0) < 0.3 and abs(r["y"]["in"]["dpi"] - 100.0) < 0.3
    # nominal 100 dpi, the page is 2 % short along x: the 150 mm baseline is ~3 mm short
    assert -3.5 < r["x"]["mm"]["length_error_mm"] < -2.5


# ------------------------------------------------------------------------------------------------ defects: mono frame, weak crosses
def test_mono_page_reports_correct_frame_clipping():
    lay, img = pages("A4", 100, {}, colour="gray")
    rep = diag.analyze_diag_page(img, lay, detector="python")
    s = rep["summary"]
    assert s["clipped_edges"] == [], s["margin_errors_mm"]
    assert rep["elements"]["printable_frame"]["mode"] == "mono"
    assert all(abs(v) < 0.3 for v in s["margin_errors_mm"].values())
    # a grey scan of the colour page behaves the same
    lay, rgb = pages("A4", 100, {})
    rep = diag.analyze_diag_page(rgb.convert("L").convert("RGB"), lay, detector="python")
    assert rep["summary"]["clipped_edges"] == []
    # cut the left edge off a mono page: the left frame edge (and only it) is clipped
    lay, img = pages("A4", 100, {}, colour="gray")
    crop = int(5.2 * 100 / 25.4)
    rep = diag.analyze_diag_page(img.crop((crop, 0, img.width, img.height)), lay, detector="python", init=(100 / 25.4, 0, -crop, 0, 100 / 25.4, 0))
    assert rep["summary"]["clipped_edges"] == ["left"], rep["summary"]["margin_errors_mm"]
    # colour pages keep working
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(img.crop((crop, 0, img.width, img.height)), lay, detector="python", init=(100 / 25.4, 0, -crop, 0, 100 / 25.4, 0))
    assert rep["summary"]["clipped_edges"] == ["left"]


def erase_crosses(img, lay, keep, k):
    out = img.copy()
    d = ImageDraw.Draw(out)
    for e in lay["elements"]:
        if e["id"].startswith("reg_") and e["id"] != keep:
            cx, cy = e["expected"]["centre_mm"]
            a = e["expected"]["arm_mm"] + 0.6
            d.rectangle([(cx - a) * k, (cy - a) * k, (cx + a) * k, (cy + a) * k], fill=(255, 255, 255))
    return out


def test_cut_off_crosses_are_rejected_not_fitted():
    lay, img = pages("A4", 100, {})
    k = lay["px_per_mm"]
    cut = erase_crosses(img, lay, "reg_tl", k)
    rep = diag.analyze_diag_page(cut, lay, detector="python", marker_fallback=False)
    assert rep["summary"]["fit"] == "failed" and rep["geometry"]["fit"] == "failed"
    reg = rep["geometry"]["registration"]
    assert reg["reg_tl"]["weak"] is False
    assert all(reg[i]["weak"] for i in ("reg_tr", "reg_bl", "reg_br"))
    assert "scale_x_vs_nominal" not in rep["summary"] and rep["elements"] == {}
    # with the marker fallback the markers (still on the page) give the fit; no clutter fit either way
    rep = diag.analyze_diag_page(cut, lay, detector="python")
    s = rep["summary"]
    assert s["fit"] == "markers" and abs(s["scale_x_vs_nominal"] - 1) < 0.003 and abs(s["rotation_deg"]) < 0.1
    # two crosses cut: still a failure, one weak cross is rejected and three survive
    two = img.copy()
    d = ImageDraw.Draw(two)
    e = {e["id"]: e for e in lay["elements"]}["reg_br"]
    cx, cy = e["expected"]["centre_mm"]
    a = e["expected"]["arm_mm"] + 0.6
    d.rectangle([(cx - a) * k, (cy - a) * k, (cx + a) * k, (cy + a) * k], fill=(255, 255, 255))
    rep = diag.analyze_diag_page(two, lay, detector="python")
    assert rep["geometry"]["registration"]["reg_br"]["weak"] and rep["summary"]["fit"] == "crosses"
    assert abs(rep["summary"]["scale_x_vs_nominal"] - 1) < 0.003


def test_clutter_in_place_of_crosses_is_not_a_fit():
    # crosses replaced by random-looking ink: the fit must not pick it up
    lay, img = pages("A4", 100, {})
    k = lay["px_per_mm"]
    junk = erase_crosses(img, lay, "reg_tl", k)
    d = ImageDraw.Draw(junk)
    for e in lay["elements"]:
        if e["id"] in ("reg_tr", "reg_bl", "reg_br"):
            cx, cy = e["expected"]["centre_mm"]
            for dx, dy in ((-2.4, 1.1), (2.9, -2.0)):
                x, y = (cx + dx) * k, (cy + dy) * k
                d.rectangle([x - 1, y - 6, x + 1, y + 6], fill=(60, 60, 60))
    rep = diag.analyze_diag_page(junk, lay, detector="python", marker_fallback=False)
    s = rep["summary"]
    if s["fit"] != "failed":  # pragma: no cover - only if the junk is accepted
        pytest.fail(f"clutter accepted: {s}")


# ------------------------------------------------------------------------------------------------ cv2 (optional)
def need_cv2():
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "aruco"):
        pytest.skip("cv2 without the aruco module")
    return cv2


@pytest.mark.parametrize("paper,dpi", [("A4", 100), ("A4", 150), ("A4", 200), ("A4", 300), ("LETTER", 100), ("A3", 100), ("LEGAL", 100), ("A5", 150)])
def test_cv2_detects_every_marker_at_every_dpi_and_paper(paper, dpi):
    need_cv2()
    lay, img = pages(paper, dpi, {})
    rep = diag.analyze_diag_page(img, lay, detector="cv2")
    m = rep["markers"]
    assert m["detector"] == "cv2"
    assert m["n_found"] == m["n_expected"] and m["missing"] == []
    ids = sorted(r["marker_id"] for r in m["per_marker"].values())
    assert ids == sorted(e["expected"]["marker_id"] for e in markers_of(lay))
    assert m["max_corner_err_mm"] < 0.6 and m["rms_corner_err_mm"] < 0.15


@pytest.mark.parametrize("name,kw", [("rotation", dict(rot=1.5)), ("scale", dict(scale=0.97)), ("crop", dict(crop=12)), ("all", dict(rot=-1.0, scale=1.02, crop=8))])
def test_cv2_after_rotation_scale_crop(name, kw):
    need_cv2()
    lay, img = pages("A4", 150, {})
    kw = dict(kw)
    if "crop" in kw:
        kw["crop"] = int(kw["crop"] * 1.5)
    rep = diag.analyze_diag_page(transform(img, **kw), lay, detector="cv2")
    assert rep["markers"]["n_found"] >= rep["markers"]["n_expected"] - 2
    assert rep["summary"]["fit"] == "crosses"
    assert rep["markers"]["max_corner_err_mm"] < 0.8


def test_cv2_flip_does_not_decode_mirrored_markers():
    need_cv2()
    lay, img = pages("A4", 150, {})
    rep = diag.analyze_diag_page(transform(img, flip="h"), lay, detector="cv2")
    assert rep["markers"]["n_found"] < rep["markers"]["n_expected"] // 2


def test_cv2_fits_a_page_whose_crosses_are_gone():
    need_cv2()
    lay, img = pages("A4", 150, {})
    k = lay["px_per_mm"]
    cut = erase_crosses(img, lay, "reg_tl", k)
    rep = diag.analyze_diag_page(cut, lay, detector="cv2")
    assert rep["summary"]["fit"] == "markers" and abs(rep["summary"]["scale_x_vs_nominal"] - 1) < 0.002


@pytest.mark.parametrize("paper,dpi", [("A4", 100), ("A4", 150), ("A4", 300), ("LETTER", 100), ("A3", 100)])
def test_cv2_finds_chessboard_charuco_and_circle_grid(paper, dpi):
    need_cv2()
    lay, img = pages(paper, dpi, {})
    rep = diag.analyze_diag_page(img, lay, detector="cv2")
    e = rep["elements"]
    ch = e["chessboard"]
    assert ch["found"] and ch["corners"] == 40 and ch["max_err_mm"] < 0.08
    cb = e["charuco_board"]
    assert cb["markers_found"] == 12 and cb["interpolated_corners"] == 15 and cb["max_err_mm"] < 0.1
    cg = e["circle_grid"]
    assert cg["cv2_found"] and cg["cv2_points"] == 30 and cg["cv2_max_err_mm"] < 0.05
    assert cg["centroids"] == 30 and cg["max_err_mm"] < 0.1


def test_boards_without_cv2_report_circles_and_say_what_is_missing():
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(img, lay, detector="python")
    assert rep["elements"]["circle_grid"]["centroids"] == 30
    assert rep["elements"]["circle_grid"]["max_err_mm"] < 0.1
    assert rep["elements"]["chessboard"]["cv2"] == "unavailable"


# ------------------------------------------------------------------------------------------------ analyzer accuracy (rev. 1 fixes)
def _touches_media_outline(lay, r, tol=1.5):
    """A dashed media-outline line (drawn on the sheet, e.g. the Statement outline on Letter) running along the marker."""
    for o in lay["elements"]:
        if o["kind"] != "media_outline":
            continue
        a = o["rect_mm"]
        if any(r[0] - tol <= x <= r[2] + tol and a[1] <= r[3] and a[3] >= r[1] for x in (a[0], a[2])):
            return True
        if any(r[1] - tol <= y <= r[3] + tol and a[0] <= r[2] and a[2] >= r[0] for y in (a[1], a[3])):
            return True
    return False


@pytest.mark.parametrize("dpi", [100, 150, 200, 300, 600])
@pytest.mark.parametrize("paper", ["A4", "LETTER", "A5"])
def test_clean_render_markers_all_found_and_worst_corner_under_a_third_of_a_pixel(paper, dpi):
    lay, img = pages(paper, dpi, {})
    k = lay["px_per_mm"]
    A = diag._Affine(k, 0.0, 0.0, 0.0, k, 0.0)  # the raster is exactly the sheet at k px/mm
    det = dm.detect_markers(img, diag._Raster(img), A, lay, detector="python")
    assert det["n_found"] == det["n_expected"], [i for i, r in det["markers"].items() if "corners_px" not in r]
    worst = 0.0
    for e in markers_of(lay):
        r = det["markers"][e["id"]]
        assert r["decoded_id"] == r["marker_id"] and r["rotation_deg"] == 0 and not r["mirrored"], e["id"]
        if _touches_media_outline(lay, e["rect_mm"]):
            continue  # real ink on the marker's edge (the generator draws the outlines of other papers over the sheet)
        worst = max(worst, max(math.dist(A(*c), q) for c, q in zip(e["expected"]["corners_mm"], r["corners_px"])))
    assert worst < 0.3, worst


def test_marker_mr_edge_fit_works_at_600_dpi():
    lay, img = pages("A4", 600, {})
    k = lay["px_per_mm"]
    det = dm.detect_markers(img, diag._Raster(img), diag._Affine(k, 0.0, 0.0, 0.0, k, 0.0), lay, detector="python")
    r = det["markers"]["marker_mr"]
    assert r["found"] and "corners_px" in r and "reason" not in r
    assert det["n_found"] == det["n_expected"] == 20


def test_marker_edges_survive_resampling_and_sub_pixel_offsets():
    # a 600 dpi render reduced to 150 dpi (area / cubic filters) and shifted by a fraction of a pixel
    lay, big = pages("A4", 600, {})
    k = 150 / 25.4
    for flt in (Image.BOX, Image.BICUBIC):
        img = big.resize((big.width // 4, big.height // 4), flt)
        img = img.transform(img.size, Image.AFFINE, (1, 0, -0.37, 0, 1, 0.21), resample=Image.BICUBIC, fillcolor=(255, 255, 255))
        A = diag._Affine(k, 0.0, 0.37, 0.0, k, -0.21)
        det = dm.detect_markers(img, diag._Raster(img), A, lay, detector="python")
        assert det["n_found"] == det["n_expected"]
        worst = max(max(math.dist(A(*c), q) for c, q in zip(e["expected"]["corners_mm"], det["markers"][e["id"]]["corners_px"]))
                    for e in markers_of(lay) if not _touches_media_outline(lay, e["rect_mm"]))
        assert worst < 0.5, (flt, worst)


def test_a_neighbouring_feature_in_one_profile_does_not_tilt_the_side():
    # robust line fit: one bad edge point among seven is dropped
    pts = [(float(i), 0.1 * i) for i in range(7)]
    pts[2] = (2.0, 3.0)
    m, q, _ = dm._fit_line(pts, by_x=True, tol=0.4)
    assert abs(m - 0.1) < 1e-9 and abs(q) < 1e-9


ALL_STATES = ["upright", "rot90", "rot180", "rot270", "flip_h", "flip_v", "transpose", "anti_transpose"]
# PIL transform that turns the upright page into a received page in each state
_MAKE = {"upright": None, "rot90": "ROTATE_270", "rot180": "ROTATE_180", "rot270": "ROTATE_90", "flip_h": "FLIP_LEFT_RIGHT",
         "flip_v": "FLIP_TOP_BOTTOM", "transpose": "TRANSPOSE", "anti_transpose": "TRANSVERSE"}


@pytest.mark.parametrize("state", ALL_STATES)
def test_detect_orientation_names_all_eight_states(state):
    lay, img = pages("A4", 100, {})
    rec = img if _MAKE[state] is None else img.transpose(getattr(Image.Transpose, _MAKE[state]))
    o = dm.detect_orientation(rec, lay)
    assert o["available"] and o["verdict"] == state and not o["ambiguous"], o
    assert o["found"] >= 6
    # the reported fix restores the upright page
    fixed = rec if o["fix"] is None else rec.transpose(getattr(Image.Transpose, o["fix"]))
    assert fixed.size == img.size


@pytest.mark.parametrize("state", ["rot180", "flip_h", "flip_v"])
def test_markers_orientation_in_the_report_names_axis_preserving_states(state):
    lay, img = pages("A4", 150, {})
    rep = diag.analyze_diag_page(img.transpose(getattr(Image.Transpose, _MAKE[state])), lay, detector="python")
    assert rep["markers"]["orientation"]["verdict"] == state
    assert rep["summary"]["marker_orientation"] == state
    assert rep["markers"]["orientation"]["fix"] is not None


@pytest.mark.parametrize("state", ["rot90", "rot270", "transpose", "anti_transpose"])
def test_axis_swapping_states_fail_the_fit_but_are_still_named(state):
    lay, img = pages("A4", 100, {})
    rep = diag.analyze_diag_page(img.transpose(getattr(Image.Transpose, _MAKE[state])), lay, detector="python")
    assert rep["summary"]["fit"] == "failed"
    assert rep["summary"]["marker_orientation"] == state


def test_scan_dpi_sets_the_plausible_scale_of_the_fit():
    # a layout generated at 400 dpi (the PDF adapter's) analysed on a 150 dpi raster
    lay400 = diag.diag_page_layout(400, paper="A4")
    img = diag.render_diag_page(150, paper="A4")
    refused = diag.analyze_diag_page(img, lay400, detector="python")
    assert refused["summary"]["fit"] == "failed" and "implausible" in refused["summary"]["reason"]
    rep = diag.analyze_diag_page(img, lay400, scan_dpi=150, detector="python")
    assert rep["summary"]["fit"] == "crosses"
    assert abs(rep["summary"]["scale_x_vs_nominal"] - 1.0) < 0.003
    assert rep["markers"]["n_found"] == rep["markers"]["n_expected"]
    assert rep["markers"]["rms_corner_err_mm"] < 0.1
    # an implausible scale relative to scan_dpi is still refused
    bad = diag.analyze_diag_page(img, lay400, scan_dpi=400, detector="python")
    assert bad["summary"]["fit"] == "failed" and "implausible" in bad["summary"]["reason"]
