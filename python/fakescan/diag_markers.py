"""ArUco markers and calibration boards of the ``diag`` page (layout v3, revision 1): decoding and sub-pixel location.

The generator (``fakescan::diag_fid``) draws OpenCV-compatible ``DICT_4X4_50`` markers; this module holds the same
dictionary (ids 0-19) so a scan can be decoded **without OpenCV**: a pure-Python detector (Pillow only) that works
from the layout (where each marker should be) and refines the outer border to a sub-pixel quad, plus an optional
``cv2.aruco`` accelerator used when ``opencv-contrib`` is importable.

Dictionary source: the OpenCV predefined dictionary ``DICT_4X4_50``.  The 16 payload bits of id ``n`` are the inner
4x4 modules of ``cv2.aruco.Dictionary.generateImageMarker(n, 6)`` read row-major from the top-left, white = 1
(id 0 is bytes 181, 50 = ``0xB532``).  A marker is 6x6 modules with a black one-module border.  The test-suite compares
every entry with ``cv2`` when it is available.

Conventions: layout coordinates are millimetres (origin top-left of the sheet); pixel ``i`` covers ``[i, i + 1)`` and
its centre is at ``i + 0.5`` (OpenCV puts the centre at integer coordinates, so cv2 corners get ``+ 0.5``).  Marker
corners are always listed TL, TR, BR, BL of the marker as drawn on the sheet.
"""

from __future__ import annotations

import math
from typing import Any

from . import diag as _d

__all__ = [
    "ARUCO_DICT",
    "ARUCO_4X4_50",
    "aruco_cells",
    "cv2_available",
    "detect_markers",
    "fit_homography",
    "analyze_boards",
    "detect_orientation",
    "ORIENTATIONS",
    "ORIENTATION_FIX",
]

ARUCO_DICT = "DICT_4X4_50"
ARUCO_4X4_50: tuple[int, ...] = (
    0xB532, 0x0F9A, 0x332D, 0x9946, 0x549E, 0x79CD, 0x9E2E, 0xC4F2, 0xFEDA, 0xCF56,
    0xF991, 0x11A7, 0x0EB7, 0x2A0F, 0x24B1, 0x263E, 0x4665, 0x6600, 0x6C5E, 0x76AF,
)  # fmt: skip


def aruco_cells(marker_id: int) -> list[list[int]]:
    """6x6 module grid of a marker, ``1`` = black (border included)."""
    code = ARUCO_4X4_50[marker_id]
    g = [[1] * 6 for _ in range(6)]
    for r in range(4):
        for c in range(4):
            g[r + 1][c + 1] = 0 if (code >> (15 - (r * 4 + c))) & 1 else 1
    return g


def _rot90(g: list[list[int]]) -> list[list[int]]:
    """Rotate 90 degrees clockwise."""
    return [list(r) for r in zip(*g[::-1])]


def _variants(g: list[list[int]]):
    """(mirrored, quarter turns clockwise, grid) for the 8 symmetries of the square."""
    out = []
    for mirrored in (False, True):
        h = [r[::-1] for r in g] if mirrored else g
        for k in range(4):
            out.append((mirrored, k, h))
            h = _rot90(h)
    return out


_TABLE: list[tuple[int, bool, int, tuple[int, ...]]] | None = None


def _table():
    global _TABLE
    if _TABLE is None:
        _TABLE = []
        for mid in range(len(ARUCO_4X4_50)):
            for mirrored, k, h in _variants(aruco_cells(mid)):
                _TABLE.append((mid, mirrored, k, tuple(v for row in h for v in row)))
    return _TABLE


def decode_grid(obs: list[list[int]]) -> dict[str, Any]:
    """Best id / symmetry for an observed 6x6 grid (1 = black): ``{id, mirrored, rotation_deg, score, margin}``."""
    flat = tuple(v for row in obs for v in row)
    best = (-1, None)
    second = -1
    per_id: dict[int, int] = {}
    cands = []
    for mid, mirrored, k, ref in _table():
        s = sum(1 for a, b in zip(flat, ref) if a == b)
        cands.append((s, mid, mirrored, k))
        if s > per_id.get(mid, -1):
            per_id[mid] = s
    # ties (a marker that equals one of its own rotations / mirrors) prefer the upright, unmirrored reading
    cands.sort(key=lambda c: (c[0], not c[2], -c[3]), reverse=True)
    best = cands[0]
    for s, mid, _, _ in cands[1:]:
        if mid != best[1]:
            second = s
            break
    return {"id": best[1], "mirrored": best[2], "rotation_deg": best[3] * 90, "score": best[0] / 36.0,
            "margin": best[0] - second}


# ------------------------------------------------------------------------------------------------ cv2 accelerator
def cv2_available() -> bool:
    try:
        import cv2  # noqa: F401

        return hasattr(cv2, "aruco")
    except Exception:  # pragma: no cover - depends on the environment
        return False


def _gray_array(image):
    import numpy as np

    return np.asarray(image.convert("L"))


def _detect_cv2(image) -> dict[int, list[list[float]]]:
    """``{marker id: 4 corners px}`` (our pixel convention) for every marker cv2 finds."""
    import cv2

    gray = _gray_array(image)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    p = cv2.aruco.DetectorParameters()
    p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    p.cornerRefinementWinSize = 3
    det = cv2.aruco.ArucoDetector(d, p)
    corners, ids, _rej = det.detectMarkers(gray)
    out: dict[int, list[list[float]]] = {}
    if ids is None:
        return out
    for c, i in zip(corners, ids.ravel()):
        out[int(i)] = [[float(x) + 0.5, float(y) + 0.5] for x, y in c.reshape(4, 2)]
    return out


# ------------------------------------------------------------------------------------------------ pure-Python detector
def _sample_module(ras, A, x: float, y: float, c: float) -> float:
    """Mean grey of a module: the centre plus four points a fifth of a module away."""
    d = 0.2 * c
    t = 0.0
    for dx, dy in ((0, 0), (d, 0), (-d, 0), (0, d), (0, -d)):
        t += ras.g(*A(x + dx, y + dy))
    return t / 5.0


def _ring_score(ras, A, x0: float, y0: float, c: float) -> float:
    """Paper level of the quiet ring minus the black level of the border ring (module centres, 8x8 grid)."""
    black = paper = 0.0
    nb = npr = 0
    for r in range(-1, 7):
        for k in range(-1, 7):
            if 1 <= r <= 4 and 1 <= k <= 4:
                continue
            v = ras.g(*A(x0 + (k + 0.5) * c, y0 + (r + 0.5) * c))
            if r in (-1, 6) or k in (-1, 6):
                paper += v
                npr += 1
            else:
                black += v
                nb += 1
    return paper / npr - black / nb


def _locate(ras, A, rect, search: float):
    """Best offset (mm) of the marker at ``rect`` within ``search`` mm, by the ring contrast.

    The ring score is flat while every module-centre sample stays inside its module (about +/- half a module), so the
    answer is the centroid of the near-maximal plateau rather than its first cell (which sat at the edge of the
    plateau and was up to half a module off).  ``_refine_corners`` then tightens the position to the edge fit.
    """
    c = (rect[2] - rect[0]) / 6.0
    step = max(0.25, min(0.6, c * 0.5))
    n = int(search / step)
    grid = {}
    for iy in range(-n, n + 1):
        for ix in range(-n, n + 1):
            grid[(ix, iy)] = _ring_score(ras, A, rect[0] + ix * step, rect[1] + iy * step, c)
    bs = max(grid.values())
    ref = min(grid.values())
    keep = [k for k, v in grid.items() if v >= bs - 0.03 * max(1.0, bs - ref)]
    best = (sum(k[0] for k in keep) / len(keep) * step, sum(k[1] for k in keep) / len(keep) * step)
    return best, bs


def _read_grid(ras, A, x0: float, y0: float, c: float):
    vals = [[_sample_module(ras, A, x0 + (k + 0.5) * c, y0 + (r + 0.5) * c, c) for k in range(6)] for r in range(6)]
    border = [vals[r][k] for r in range(6) for k in range(6) if r in (0, 5) or k in (0, 5)]
    paper = []
    for k in range(6):
        paper.append(ras.g(*A(x0 + (k + 0.5) * c, y0 - 0.5 * c)))
        paper.append(ras.g(*A(x0 + (k + 0.5) * c, y0 + 6.5 * c)))
        paper.append(ras.g(*A(x0 - 0.5 * c, y0 + (k + 0.5) * c)))
        paper.append(ras.g(*A(x0 + 6.5 * c, y0 + (k + 0.5) * c)))
    b = sum(border) / len(border)
    p = sum(paper) / len(paper)
    thr = (b + p) / 2.0
    return [[1 if v < thr else 0 for v in row] for row in vals], p - b


def _edge_cross(profile: list[float], step: float, lo_mm: float):
    """Position (same unit as ``step``) where ink (start of the profile) ends and paper begins.

    The edge is the integral of the ink fraction ``f = (paper - v) / (paper - ink)`` along the profile: for any
    symmetric blur and for the area-averaged edge of a rasterised page this equals the true edge position (a
    mid-level crossing of the linearly interpolated profile is biased by up to a quarter pixel, depending on the
    sub-pixel phase of the edge).  Returns ``None`` when there is not enough contrast or the ends are not clean.
    """
    n = len(profile)
    k = max(2, n // 4)
    ink = sum(profile[:k]) / k
    paper = sum(profile[-k:]) / k
    if paper - ink < 40:
        return None
    f = [min(1.0, max(0.0, (paper - v) / (paper - ink))) for v in profile]
    if f[0] < 0.7 or f[-1] > 0.3:
        return None
    area = (sum(f) - 0.5 * (f[0] + f[-1])) * step
    return lo_mm + area


def _fit_line(pts: list[tuple[float, float]], by_x: bool, tol: float | None = None):
    """Least squares ``y = m x + q`` (``by_x``) or ``x = m y + q``; returns (m, q, rms).

    With ``tol`` the fit is robust: a Theil-Sen line (median pairwise slope) first, then points further than ``tol``
    from it are dropped before the least squares (an edge profile that crosses a neighbouring feature gives one bad
    point, which must not tilt the side).  At least four points are kept.
    """
    xs = [p[0] if by_x else p[1] for p in pts]
    ys = [p[1] if by_x else p[0] for p in pts]
    if tol is not None and len(xs) > 4:
        slopes = sorted((ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs))
                        if xs[j] != xs[i])
        if slopes:
            m0 = slopes[len(slopes) // 2]
            res = sorted(y - m0 * x for x, y in zip(xs, ys))
            q0 = res[len(res) // 2]
            keep = [i for i in range(len(xs)) if abs(ys[i] - (m0 * xs[i] + q0)) <= tol]
            if len(keep) >= 4:
                xs = [xs[i] for i in keep]
                ys = [ys[i] for i in keep]
    m, q, rms = _d._linfit(xs, ys)
    return m, q, rms


def _refine_corners(ras, A, rect):
    """Sub-pixel outer corners (mm) of the marker border from edge profiles on the four sides, or ``None``.

    Profiles run from inside the one-module black border (0.45 module in, so the white payload modules next to it
    are never sampled even when ``rect`` is a few tenths of a module off) to 0.8 module outside, at quarter-pixel
    spacing; each is reduced to an edge position by ``_edge_cross`` and the four sides are fitted by lines.
    """
    x0, y0, x1, y1 = rect
    c = (x1 - x0) / 6.0
    ppm = math.sqrt(abs(A.a * A.e - A.b * A.d)) or 1.0
    inside, outside = 0.45 * c, 0.8 * c
    step = 0.25 / ppm
    n = int((inside + outside) / step) + 1
    sides = {
        # (point on side at t, outward normal)
        "top": (lambda t: (x0 + t * (x1 - x0), y0), (0.0, -1.0)),
        "bottom": (lambda t: (x0 + t * (x1 - x0), y1), (0.0, 1.0)),
        "left": (lambda t: (x0, y0 + t * (y1 - y0)), (-1.0, 0.0)),
        "right": (lambda t: (x1, y0 + t * (y1 - y0)), (1.0, 0.0)),
    }
    lines = {}
    rms_all = []
    for name, (pt, nrm) in sides.items():
        pts = []
        for t in (0.22, 0.32, 0.42, 0.5, 0.58, 0.68, 0.78):
            bx, by = pt(t)
            # profile from inside the border (ink) to outside (paper)
            prof = [ras.g(*A(bx + nrm[0] * (-inside + i * step), by + nrm[1] * (-inside + i * step))) for i in range(n)]
            s = _edge_cross(prof, step, -inside)
            if s is not None:
                pts.append((bx + nrm[0] * s, by + nrm[1] * s))
        if len(pts) < 4:
            return None
        horizontal = name in ("top", "bottom")
        m, q, rms = _fit_line(pts, by_x=horizontal, tol=0.4 / ppm)
        lines[name] = (horizontal, m, q)
        rms_all.append(rms)

    def inter(h, v):
        # h: y = m1 x + q1 ; v: x = m2 y + q2
        _, m1, q1 = h
        _, m2, q2 = v
        det = 1.0 - m1 * m2
        x = (m2 * q1 + q2) / det
        y = m1 * x + q1
        return [x, y]

    corners = [inter(lines["top"], lines["left"]), inter(lines["top"], lines["right"]),
               inter(lines["bottom"], lines["right"]), inter(lines["bottom"], lines["left"])]
    return corners, max(rms_all)


def _refine_rect(ras, A, x0: float, y0: float, c: float, passes: int = 3):
    """Iterate ``_refine_corners`` re-centring on the fitted quad; returns ``(corners, rms, x0, y0)`` or ``None``."""
    res = None
    for _ in range(passes):
        ref = _refine_corners(ras, A, [x0, y0, x0 + 6 * c, y0 + 6 * c])
        if ref is None:
            break
        corners, rms = ref
        res = (corners, rms, x0, y0)
        cx = sum(p[0] for p in corners) / 4.0
        cy = sum(p[1] for p in corners) / 4.0
        nx, ny = cx - 3 * c, cy - 3 * c
        if math.hypot(nx - x0, ny - y0) < 0.02 * c:
            x0, y0 = nx, ny
            break
        x0, y0 = nx, ny
    return res


def _detect_python(ras, A, markers: list[dict[str, Any]], search: float) -> dict[str, dict[str, Any]]:
    """Locate every drawn marker element of a layout near where ``A`` predicts it."""
    out: dict[str, dict[str, Any]] = {}
    for el in markers:
        rect = el["rect_mm"]
        c = (rect[2] - rect[0]) / 6.0
        (ox, oy), score = _locate(ras, A, rect, search)
        x0, y0 = rect[0] + ox, rect[1] + oy
        ref = _refine_rect(ras, A, x0, y0, c)
        if ref is not None:
            # read the payload at the edge-fitted position (the ring locator is only good to a fraction of a module)
            x0, y0 = ref[2], ref[3]
            cx = sum(p[0] for p in ref[0]) / 4.0
            cy = sum(p[1] for p in ref[0]) / 4.0
            x0, y0 = cx - 3 * c, cy - 3 * c
        grid, contrast = _read_grid(ras, A, x0, y0, c)
        dec = decode_grid(grid)
        found = dec["score"] >= 0.92 and dec["margin"] >= 2 and contrast > 40
        rec: dict[str, Any] = {"found": bool(found), "ring_contrast": round(score, 1), "decoded": dec,
                               "offset_mm": [round(x0 - rect[0], 3), round(y0 - rect[1], 3)]}
        if found and ref is not None:
            rec["corners_mm"] = ref[0]
            rec["edge_fit_rms_mm"] = ref[1]
        out[el["id"]] = rec
    return out


# ------------------------------------------------------------------------------------------------ geometry helpers
def fit_homography(mm: list[tuple[float, float]], px: list[tuple[float, float]]):
    """Direct linear transform mm -> px with Hartley normalisation; returns the 3x3 matrix (list of rows) or None."""
    n = len(mm)
    if n < 4:
        return None

    def norm(pts):
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        s = math.sqrt(2.0) / max(1e-12, sum(math.hypot(p[0] - cx, p[1] - cy) for p in pts) / len(pts))
        return [[s, 0, -s * cx], [0, s, -s * cy], [0, 0, 1]], [((p[0] - cx) * s, (p[1] - cy) * s) for p in pts]

    Tm, mn = norm(mm)
    Tp, pn = norm(px)
    # normal equations of the 8-parameter system (h33 = 1)
    ata = [[0.0] * 8 for _ in range(8)]
    atb = [0.0] * 8
    for (x, y), (u, v) in zip(mn, pn):
        for row, rhs in (([x, y, 1, 0, 0, 0, -u * x, -u * y], u), ([0, 0, 0, x, y, 1, -v * x, -v * y], v)):
            for i in range(8):
                atb[i] += row[i] * rhs
                for j in range(8):
                    ata[i][j] += row[i] * row[j]
    h = _solve(ata, atb)
    if h is None:
        return None
    Hn = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1.0]]
    Tp_inv = [[1 / Tp[0][0], 0, -Tp[0][2] / Tp[0][0]], [0, 1 / Tp[1][1], -Tp[1][2] / Tp[1][1]], [0, 0, 1]]
    return _mul(Tp_inv, _mul(Hn, Tm))


def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _solve(m: list[list[float]], r: list[float]):
    n = len(r)
    m = [row[:] for row in m]
    r = r[:]
    for i in range(n):
        piv = max(range(i, n), key=lambda k: abs(m[k][i]))
        if abs(m[piv][i]) < 1e-12:
            return None
        m[i], m[piv] = m[piv], m[i]
        r[i], r[piv] = r[piv], r[i]
        for k in range(i + 1, n):
            q = m[k][i] / m[i][i]
            for j in range(i, n):
                m[k][j] -= q * m[i][j]
            r[k] -= q * r[i]
    out = [0.0] * n
    for i in range(n - 1, -1, -1):
        out[i] = (r[i] - sum(m[i][j] * out[j] for j in range(i + 1, n))) / m[i][i]
    return out


def _apply_h(H, x: float, y: float):
    w = H[2][0] * x + H[2][1] * y + H[2][2]
    return (H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w


# ------------------------------------------------------------------------------------------------ public detector
def _expected_pairs(markers: list[dict[str, Any]]):
    return {m["id"]: [tuple(p) for p in m["expected"]["corners_mm"]] for m in markers}


def detect_markers(image, ras, A, layout: dict[str, Any], detector: str = "auto", search_mm: float = 3.0,
                   ) -> dict[str, Any]:
    """Detect every drawn ArUco marker of a v3 layout.

    ``A`` is the current mm -> px affine (``None`` = nominal scale, only usable with the cv2 detector which needs no
    seed).  ``detector``: ``"auto"`` (cv2 when importable, else Python), ``"cv2"`` or ``"python"``.
    Returns ``{"detector", "markers": {element id: {...}}, "n_expected", "n_found", ...}``; per marker ``corners_px`` (TL, TR,
    BR, BL as drawn), ``err_px`` / ``err_mm`` per corner against the prediction of ``A`` (when given) and the decoded id,
    rotation and mirror state.
    """
    els = [e for e in layout["elements"] if e.get("kind") == "marker" and e.get("drawn", True)]
    use = detector
    if use == "auto":
        use = "cv2" if cv2_available() else "python"
    if use == "cv2" and not cv2_available():
        use = "python"
    res: dict[str, Any] = {"detector": use, "markers": {}, "n_expected": len(els)}
    if use == "cv2":
        found = _detect_cv2(image)
        for e in els:
            mid = e["expected"]["marker_id"]
            rec: dict[str, Any] = {"found": mid in found, "marker_id": mid}
            if mid in found:
                rec["corners_px"] = found[mid]
                rec["rotation_deg"] = 0
                rec["mirrored"] = False
            res["markers"][e["id"]] = rec
        res["unexpected_ids"] = sorted(set(found) - {e["expected"]["marker_id"] for e in els})
    else:
        if A is None:
            raise ValueError("the pure-Python marker detector needs an initial affine")
        raw = _detect_python(ras, A, els, search_mm)
        for e in els:
            r = raw[e["id"]]
            rec = {"found": r["found"], "marker_id": e["expected"]["marker_id"], "decoded_id": r["decoded"]["id"],
                   "rotation_deg": r["decoded"]["rotation_deg"], "mirrored": r["decoded"]["mirrored"],
                   "match": r["decoded"]["score"], "ring_contrast": r["ring_contrast"]}
            if r["found"] and "corners_mm" in r:
                rec["corners_px"] = [list(A(*c)) for c in r["corners_mm"]]
                rec["edge_fit_rms_mm"] = r["edge_fit_rms_mm"]
            elif r["found"]:
                rec["found"] = False
                rec["reason"] = "edge fit failed"
            res["markers"][e["id"]] = rec
    res["n_found"] = sum(1 for r in res["markers"].values() if r["found"] and "corners_px" in r)
    return res


def marker_correspondences(det: dict[str, Any], layout: dict[str, Any]):
    """(mm points, px points, element ids) of every detected marker corner."""
    pairs = _expected_pairs([e for e in layout["elements"] if e.get("kind") == "marker" and e.get("drawn", True)])
    els = {e["id"]: e for e in layout["elements"]}
    mm, px, who = [], [], []
    for eid, rec in det["markers"].items():
        if not rec.get("found") or "corners_px" not in rec:
            continue
        # a mirrored / rotated detection that decoded to another id is not a position match
        if rec.get("decoded_id") is not None and rec["decoded_id"] != rec["marker_id"]:
            continue
        exp = pairs[eid] if eid in pairs else [tuple(p) for p in els[eid]["expected"]["corners_mm"]]
        for e, p in zip(exp, rec["corners_px"]):
            mm.append(e)
            px.append(tuple(p))
            who.append(eid)
    return mm, px, who


def marker_report(det: dict[str, Any], layout: dict[str, Any], A) -> dict[str, Any]:
    """Per-marker position errors against ``A`` plus the affine / homography summary of all marker corners."""
    nominal = layout["px_per_mm"]
    mm, px, who = marker_correspondences(det, layout)
    per: dict[str, Any] = {}
    errs_all = []
    for eid, rec in det["markers"].items():
        out = {k: rec[k] for k in ("found", "marker_id", "decoded_id", "rotation_deg", "mirrored", "match") if k in rec}
        if rec.get("found") and "corners_px" in rec and A is not None:
            exp = [e for e in layout["elements"] if e["id"] == eid][0]["expected"]["corners_mm"]
            e_mm = []
            for c, p in zip(exp, rec["corners_px"]):
                dm = A.inverse(p[0], p[1])
                e_mm.append(math.dist(dm, c))
            out["corner_err_mm"] = [round(v, 4) for v in e_mm]
            out["max_err_mm"] = max(e_mm)
            out["mean_err_mm"] = sum(e_mm) / 4
            out["max_err_px"] = max(e_mm) * math.hypot(A.a, A.d)
            ctr_det = [sum(p[0] for p in rec["corners_px"]) / 4, sum(p[1] for p in rec["corners_px"]) / 4]
            ctr_exp = A(sum(c[0] for c in exp) / 4, sum(c[1] for c in exp) / 4)
            out["centre_err_px"] = math.dist(ctr_det, ctr_exp)
            errs_all.extend(e_mm)
        per[eid] = out
    rep: dict[str, Any] = {
        "detector": det["detector"],
        "n_expected": det["n_expected"],
        "n_found": det["n_found"],
        "missing": sorted(k for k, v in per.items() if not v.get("found")),
        "per_marker": per,
    }
    if errs_all:
        rep["rms_corner_err_mm"] = math.sqrt(sum(v * v for v in errs_all) / len(errs_all))
        rep["max_corner_err_mm"] = max(errs_all)
        rep["rms_corner_err_px"] = rep["rms_corner_err_mm"] * nominal
    if len(mm) >= 4:
        # affine from the marker corners alone: scale x / y, rotation, flip, residual in mm
        Am = _d._Affine.fit(mm, px)
        dec = Am.decompose()
        res_px = [math.dist(Am(*m), p) for m, p in zip(mm, px)]
        sx, sy = dec["px_per_mm_x"], dec["px_per_mm_y"]
        det_lin = Am.a * Am.e - Am.b * Am.d
        rep["affine"] = {
            "px_per_mm_x": sx, "px_per_mm_y": sy, "dpi_x": sx * 25.4, "dpi_y": sy * 25.4,
            "rotation_deg": dec["rotation_deg"], "skew_deg": dec["skew_deg"],
            "mirrored": det_lin < 0, "residual_rms_mm": math.sqrt(sum(r * r for r in res_px) / len(res_px)) / ((sx + sy) / 2),
            "residual_max_mm": max(res_px) / ((sx + sy) / 2), "matrix": [Am.a, Am.b, Am.c, Am.d, Am.e, Am.f],
        }
        H = fit_homography(mm, px)
        if H is not None:
            scale = (sx + sy) / 2.0
            hres = [math.dist(_apply_h(H, *m), p) / scale for m, p in zip(mm, px)]
            rep["homography"] = {"matrix": H, "residual_rms_mm": math.sqrt(sum(r * r for r in hres) / len(hres)),
                                 "residual_max_mm": max(hres), "points": len(hres)}
    # page transform from the markers that decoded as another id / orientation
    rep["orientation"] = _orientation_from_markers(det, layout)
    return rep


#: the eight dihedral states of a received page (what happened to the page, not the fix):
#: name -> (mirrored, clockwise rotation in degrees) as ``decode_grid`` reports it for a marker of that page.  ``rotN``
#: is the page rotated N degrees clockwise; ``transpose`` is mirrored along the top-left to bottom-right diagonal
#: (PIL ``Image.TRANSPOSE``), ``anti_transpose`` along the top-right to bottom-left one (``Image.TRANSVERSE``).
ORIENTATIONS: dict[str, tuple[bool, int]] = {
    "upright": (False, 0), "rot90": (False, 90), "rot180": (False, 180), "rot270": (False, 270),
    "flip_h": (True, 0), "anti_transpose": (True, 90), "flip_v": (True, 180), "transpose": (True, 270),
}
#: PIL ``Image.Transpose`` member that turns a received page of each state back into the upright one (``None`` = none)
ORIENTATION_FIX: dict[str, str | None] = {
    "upright": None, "rot90": "ROTATE_90", "rot180": "ROTATE_180", "rot270": "ROTATE_270",
    "flip_h": "FLIP_LEFT_RIGHT", "flip_v": "FLIP_TOP_BOTTOM", "transpose": "TRANSPOSE", "anti_transpose": "TRANSVERSE",
}
_BY_PATTERN = {v: k for k, v in ORIENTATIONS.items()}


def _where(name: str, x: float, y: float, w: float, h: float) -> tuple[float, float]:
    """Where an upright sheet position ``(x, y)`` lands on a ``w`` x ``h`` mm sheet received in state ``name``."""
    return {
        "upright": (x, y), "flip_h": (w - x, y), "flip_v": (x, h - y), "rot180": (w - x, h - y),
        "rot90": (h - y, x), "rot270": (y, w - x), "transpose": (y, x), "anti_transpose": (h - y, w - x),
    }[name]


def _orientation_from_markers(det: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    """Dihedral state of the page from the detected markers, whatever ids they decoded as.

    Each decoded marker carries the transform of the page in its own module pattern (``mirrored`` + clockwise
    rotation), and its decoded id names where on an upright sheet that marker belongs.  Every one of the eight states
    is scored by (a) the markers whose pattern transform equals it and (b) the markers whose decoded id sits, under
    that state, where the marker was found.  A page rotated 180 degrees therefore reads ``rot180`` although every
    marker decodes as a different id.  ``verdict`` is ``unknown`` when no marker was read.
    """
    els = {e["id"]: e for e in layout["elements"] if e.get("kind") == "marker"}
    centre = {}
    by_mid = {}
    for e in els.values():
        mid = (e.get("expected") or {}).get("marker_id")
        if not e.get("drawn", True) or mid is None:
            continue
        r = e["rect_mm"]
        centre[e["id"]] = ((r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0)
        by_mid[mid] = e["id"]
    w, h = layout["raster_mm"]
    tol = 0.1 * max(w, h) / 10.0 + 1.0
    pattern = {n: 0 for n in ORIENTATIONS}
    position = {n: 0 for n in ORIENTATIONS}
    read = other = 0
    for eid, rec in det["markers"].items():
        if not rec.get("found") or rec.get("decoded_id") is None:
            continue
        read += 1
        if rec["decoded_id"] != rec.get("marker_id"):
            other += 1
        key = (bool(rec.get("mirrored")), int(rec.get("rotation_deg", 0)) % 360)
        if key in _BY_PATTERN:
            pattern[_BY_PATTERN[key]] += 1
        src = by_mid.get(rec["decoded_id"])
        if src is None or eid not in centre:
            continue
        for name in ORIENTATIONS:
            px, py = _where(name, *centre[src], w, h)
            if math.hypot(px - centre[eid][0], py - centre[eid][1]) <= tol:
                position[name] += 1
    if not read:
        return {"verdict": "unknown", "other_id_markers": other, "votes": 0}
    score = {n: pattern[n] + position[n] for n in ORIENTATIONS}
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0] != "upright"))
    best, n = ranked[0]
    mirrored, rot = ORIENTATIONS[best]
    return {"verdict": best, "mirrored": mirrored, "rotation_deg": rot, "votes": pattern[best],
            "position_votes": position[best], "other_id_markers": other, "fix": ORIENTATION_FIX[best],
            "ambiguous": n == 0 or ranked[1][1] == n}


def detect_orientation(image, layout: dict[str, Any], scan_dpi: float | None = None, search_mm: float = 3.0) -> dict[str, Any]:
    """Which of the eight dihedral states a received page is in, from the layout's eight edge / corner markers alone.

    Unlike the per-marker vote of ``marker_report`` this needs no page fit, so it also names the states that swap the
    sheet's axes (``rot90``, ``rot270``, ``transpose``, ``anti_transpose``): the raster is transformed by each
    candidate fix, the markers are decoded where an upright page has them (pure-Python detector, seeded by the raster
    size over the sheet size) and the fix under which most of them decode as themselves, unrotated and unmirrored,
    wins.  ``scan_dpi`` is ignored when the raster covers the whole sheet (the usual case).  Returns ``{"available":
    False}`` for a layout without markers.
    """
    from PIL import Image

    marks = [e for e in layout["elements"] if e.get("kind") == "marker" and e.get("drawn", True) and e["id"].startswith("marker_")]
    if len(marks) < 3:
        return {"available": False, "verdict": None}
    sub = dict(layout, elements=marks)
    rw, rh = layout["raster_mm"]
    scored = []
    for name in ORIENTATIONS:
        fix = ORIENTATION_FIX[name]
        cand = image if fix is None else image.transpose(getattr(Image.Transpose, fix))
        ras = _d._Raster(cand)
        A = _d._Affine(cand.size[0] / rw, 0.0, 0.0, 0.0, cand.size[1] / rh, 0.0)
        det = detect_markers(cand, ras, A, sub, detector="python", search_mm=search_mm)
        n = sum(1 for r in det["markers"].values()
                if r.get("found") and r.get("decoded_id") == r.get("marker_id") and r.get("rotation_deg") == 0 and not r.get("mirrored"))
        scored.append((n, name))
    scored.sort(key=lambda t: (-t[0], t[1] != "upright"))
    top, second = scored[0], scored[1]
    mirrored, rot = ORIENTATIONS[top[1]]
    return {"available": True, "verdict": top[1], "fix": ORIENTATION_FIX[top[1]], "rotation_deg": rot, "mirrored": mirrored,
            "found": top[0], "runner_up_found": second[0], "ambiguous": top[0] < 2 or second[0] >= top[0]}


# ------------------------------------------------------------------------------------------------ boards
def _crop_box(A, rect, margin: float, w: int, h: int):
    pts = [A(x, y) for x in (rect[0] - margin, rect[2] + margin) for y in (rect[1] - margin, rect[3] + margin)]
    x0 = max(0, int(math.floor(min(p[0] for p in pts))))
    y0 = max(0, int(math.floor(min(p[1] for p in pts))))
    x1 = min(w, int(math.ceil(max(p[0] for p in pts))))
    y1 = min(h, int(math.ceil(max(p[1] for p in pts))))
    return x0, y0, x1, y1


def _match_points(found_px, expected_mm, A):
    """Mean / max / rms distance (mm) of detected px points to the nearest expected mm point (order independent)."""
    errs = []
    used = set()
    for p in found_px:
        m = A.inverse(*p)
        j = min((k for k in range(len(expected_mm)) if k not in used), key=lambda k: math.dist(m, expected_mm[k]), default=None)
        if j is None:
            break
        used.add(j)
        errs.append(math.dist(m, expected_mm[j]))
    return errs


def _stats(errs: list[float]) -> dict[str, Any]:
    if not errs:
        return {}
    return {"mean_err_mm": sum(errs) / len(errs), "max_err_mm": max(errs), "rms_err_mm": math.sqrt(sum(e * e for e in errs) / len(errs))}


def _circle_centroids(ras, A, layout_el) -> dict[str, Any]:
    ex = layout_el["expected"]
    r = ex["circle_diameter_mm"] / 2.0
    errs = []
    for cx, cy in ex["centres_mm"]:
        win = r * 1.5
        u0, v0 = A(cx, cy)
        hx, hy = (abs(A.a) + abs(A.b)) * win, (abs(A.d) + abs(A.e)) * win
        xs0, xs1 = int(u0 - hx), int(u0 + hx) + 1
        ys0, ys1 = int(v0 - hy), int(v0 + hy) + 1
        vals = [(x, y, 255.0 - ras.gi(x, y)) for y in range(ys0, ys1) for x in range(xs0, xs1)]
        lo = min(v for _, _, v in vals)
        sw = sx = sy = 0.0
        for x, y, v in vals:
            w = v - lo
            sw += w
            sx += w * (x + 0.5)
            sy += w * (y + 0.5)
        if sw <= 0:
            continue
        mm = A.inverse(sx / sw, sy / sw)
        errs.append(math.dist(mm, (cx, cy)))
    return {"centroids": len(errs), **_stats(errs)}


def analyze_boards(image, ras, A, layout: dict[str, Any], use_cv2: bool = True) -> dict[str, Any]:
    """chessboard / ChArUco / circle-grid reports.  Circle centroids are pure Python; corner interpolation needs cv2."""
    out: dict[str, Any] = {}
    els = {e["id"]: e for e in layout["elements"] if e.get("drawn", True)}
    cv = cv2_available() and use_cv2
    if "circle_grid" in els:
        rep = _circle_centroids(ras, A, els["circle_grid"])
        if cv:
            rep.update(_cv2_circles(image, A, els["circle_grid"]))
        out["circle_grid"] = rep
    if cv and "chessboard" in els:
        out["chessboard"] = _cv2_chessboard(image, A, els["chessboard"])
    if cv and "charuco_board" in els:
        out["charuco_board"] = _cv2_charuco(image, A, els["charuco_board"])
    if not cv:
        for k in ("chessboard", "charuco_board"):
            if k in els:
                out[k] = {"cv2": "unavailable", "note": "corner interpolation needs opencv-contrib; ChArUco markers are still reported"}
    return out


def _cv2_circles(image, A, el) -> dict[str, Any]:
    import cv2
    import numpy as np

    ex = el["expected"]
    gray = _gray_array(image)
    x0, y0, x1, y1 = _crop_box(A, el["rect_mm"], ex["quiet_mm"] * 0.9, gray.shape[1], gray.shape[0])
    crop = np.ascontiguousarray(gray[y0:y1, x0:x1])
    ok, pts = cv2.findCirclesGrid(crop, (ex["per_row"], ex["rows"]), flags=cv2.CALIB_CB_ASYMMETRIC_GRID)
    rep: dict[str, Any] = {"cv2_found": bool(ok)}
    if ok:
        found = [(float(p[0][0]) + x0 + 0.5, float(p[0][1]) + y0 + 0.5) for p in pts]
        rep["cv2_points"] = len(found)
        rep.update({"cv2_" + k: v for k, v in _stats(_match_points(found, [tuple(c) for c in ex["centres_mm"]], A)).items()})
    return rep


def _cv2_chessboard(image, A, el) -> dict[str, Any]:
    import cv2
    import numpy as np

    ex = el["expected"]
    gray = _gray_array(image)
    x0, y0, x1, y1 = _crop_box(A, el["rect_mm"], ex["quiet_mm"] * 0.9, gray.shape[1], gray.shape[0])
    crop = np.ascontiguousarray(gray[y0:y1, x0:x1])
    n = tuple(ex["inner_corners"])
    ok, pts = cv2.findChessboardCorners(crop, n, flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE)
    rep: dict[str, Any] = {"found": bool(ok), "expected_corners": n[0] * n[1]}
    if ok:
        sq_px = ex["square_mm"] * math.hypot(A.a, A.d)
        win = max(2, int(sq_px / 4))
        pts = cv2.cornerSubPix(crop, pts, (win, win), (-1, -1), (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 0.001))
        found = [(float(p[0][0]) + x0 + 0.5, float(p[0][1]) + y0 + 0.5) for p in pts]
        rep["corners"] = len(found)
        rep.update(_stats(_match_points(found, [tuple(c) for c in ex["inner_corner_positions_mm"]], A)))
    return rep


def _cv2_charuco(image, A, el) -> dict[str, Any]:
    import cv2
    import numpy as np

    ex = el["expected"]
    gray = _gray_array(image)
    x0, y0, x1, y1 = _crop_box(A, el["rect_mm"], ex["quiet_mm"] * 0.9, gray.shape[1], gray.shape[0])
    crop = np.ascontiguousarray(gray[y0:y1, x0:x1])
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    cols, rows = ex["squares"]
    board = cv2.aruco.CharucoBoard((cols, rows), ex["square_mm"], ex["marker_mm"], d, np.array(ex["marker_ids"], dtype=np.int32))
    prm = cv2.aruco.DetectorParameters()
    prm.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    prm.cornerRefinementWinSize = 3
    det = cv2.aruco.CharucoDetector(board, cv2.aruco.CharucoParameters(), prm)
    corners, ids, mcorners, mids = det.detectBoard(crop)
    rep: dict[str, Any] = {"expected_corners": ex["corner_count"], "markers_found": 0 if mids is None else int(len(mids))}
    if ids is None or len(ids) == 0:
        rep["interpolated_corners"] = 0
        return rep
    errs = []
    for p, i in zip(corners.reshape(-1, 2), ids.ravel()):
        # measured with cv2 4.13: interpolated ChArUco corners are already in the pixel-edge convention (no + 0.5)
        mm = A.inverse(float(p[0]) + x0, float(p[1]) + y0)
        errs.append(math.dist(mm, ex["chessboard_corners_mm"][int(i)]))
    rep["interpolated_corners"] = len(errs)
    rep.update(_stats(errs))
    return rep
