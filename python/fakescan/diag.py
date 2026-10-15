"""The fakescan ``diag`` test page: ground-truth layout, rendering and a per-element analyzer.

The default is layout v3 (``docs/diag-page-v3.md``): a parametric generator that takes the sheet size (paper name or
mm), dpi, margins, page / copy / side and returns the layout JSON, vector primitives and raster for any size; its
analyzer is :mod:`fakescan.diag3`.  The text below describes the fixed-size layout v2 (``version=2``).

Layout v2 of the page (see ``docs/diag-page-v2.md`` in fakescan) carries print marks, rulers, resolution
targets, colour targets and geometry (Sierpinski triangle, golden construction, circles, hexagon, right-angle
markers) in the areas layout v1 leaves empty.  This module is the established Python entry point for it:

* :func:`diag_page_layout` - ground truth for every element (position / size in mm in the page coordinate
  system, expected values, sample points), straight from the Rust generator so it cannot drift;
* :func:`render_diag_page` / :func:`render_diag_png` - the page as a PIL image / PNG bytes (sRGB declared);
* :func:`analyze_diag_page` - given a scanned or printed-back raster and the layout, a per-element report:
  affine page fit (scale x/y, rotation, shear, margin offset), frames (which edges survived), rulers (dpi per
  axis from the tick pitch, length error over the long baseline), colour ramps (RGB / delta E error, clip
  detection), hue-wheel hue error, resolution (last resolvable pitch, Siemens star limit, slanted-edge
  rise), geometry (golden-construction scale, ellipse fits of the circles, marker angles, smallest resolvable
  Sierpinski level) and text legibility.

Coordinate system: millimetres, origin top-left of the 206 x 288 mm layout box, x right, y down.  Angles in
degrees; ``rotation_deg`` is positive clockwise on screen.  The page is assumed upright (use the blind
``pdfcraft_fakescan.pattern_analyzer`` first when orientation or bit-order faults are possible).  Pure Python
plus Pillow (Pillow is imported lazily; it is a dev dependency of fakescan-python).
"""

from __future__ import annotations

import colorsys
import json
import math
from typing import Any

__all__ = [
    "diag_page_layout",
    "render_diag_page",
    "render_diag_png",
    "analyze_diag_page",
    "elements_by_id",
    "diag_page_primitives",
    "diag_spec_from_qr",
    "diag_paper_sizes",
    "srgb_to_lab",
    "delta_e76",
]

PAGE_LAYOUT_VERSION = 3


# --------------------------------------------------------------------------------------------------
# Generator access
# --------------------------------------------------------------------------------------------------
def _params(**kw: Any) -> str | None:
    kw = {k: v for k, v in kw.items() if v is not None}
    return json.dumps(kw) if kw else None


def diag_page_layout(dpi: int = 100, version: int = PAGE_LAYOUT_VERSION, **params: Any) -> dict[str, Any]:
    """Ground-truth layout (and vector primitives) of the ``diag`` page.

    Version 3 (default) is the parametric generator: ``paper`` (``"A4"``, ``"LETTER"``, ``"A3"``, ``"JIS-B5"``,
    ``"210x297"``, ...), ``orientation``, ``width_mm`` / ``height_mm`` (custom), ``width_px`` / ``height_px`` (exact
    raster), ``margin_mm``, ``bleed_mm``, ``page``, ``copy``, ``total``, ``side`` (``front`` / ``back``), ``job``,
    ``colour`` (``rgb`` / ``gray``), ``rulers`` (``metric`` / ``inch`` / ``both``, default ``both``), ``banner_px``.  Versions 1 and 2 are the fixed-size layouts (``dpi`` is the
    requested dpi; the page renders at the effective dpi).
    """
    from . import _native

    return json.loads(_native.diag_layout_json(dpi, version, _params(**params)))


def render_diag_png(dpi: int = 100, page: int = 0, version: int = PAGE_LAYOUT_VERSION, **params: Any) -> bytes:
    """PNG bytes of the page with the sRGB colour space (and, for v3, the physical size) declared."""
    from . import _native

    return bytes(_native.diag_render_png(dpi, page, version, _params(**params)))


def render_diag_page(dpi: int = 100, page: int = 0, version: int = PAGE_LAYOUT_VERSION, **params: Any):
    """The page as an RGB ``PIL.Image`` (needs Pillow); same keyword parameters as :func:`diag_page_layout`."""
    from PIL import Image

    from . import _native

    w, h, data = _native.diag_render_rgb(dpi, page, version, _params(**params))
    return Image.frombytes("RGB", (w, h), bytes(data))


def diag_page_primitives(layout: dict[str, Any]) -> list[dict[str, Any]]:
    """Vector drawing primitives of a v3 layout (mm, page coordinates, in paint order).

    ``op`` is one of rect, polygon, line, circle, text, grating, ticks (``vector: true``: draw them with any
    vector backend) or ramp, hue_wheel, siemens_star, zone_plate, halftone, qr (``vector: false``: raster-only, crop
    the element rectangle from :func:`render_diag_page`, or draw the QR yourself from ``payload``).  The ArUco markers,
    boards, brackets and vertical ruler ticks (layout revision 1) are plain ``rect`` / ``circle`` ops: no new op needed.
    """
    return list(layout.get("primitives", []))


def diag_spec_from_qr(payload: str) -> dict[str, Any] | None:
    """Page parameters (as accepted by :func:`diag_page_layout`) from a v3 QR payload, or ``None``."""
    from . import _native

    r = _native.diag_spec_from_qr(payload)
    if not r:
        return None
    spec = json.loads(r)
    # layout revision inside layout 3 (the payload key ``rev``; payloads without it are revision 0)
    rev = 0
    for part in payload.split("|"):
        if part.startswith("rev="):
            try:
                rev = int(part[4:])
            except ValueError:
                rev = 0
    spec["layout_revision"] = rev
    return spec


def diag_paper_sizes() -> dict[str, tuple[float, float]]:
    """Named paper sizes in mm (portrait width, height); ``WxH`` custom sizes are always accepted."""
    from . import _native

    return {n: (w, h) for n, w, h in _native.diag_paper_sizes()}


def elements_by_id(layout: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["id"]: e for e in layout["elements"]}


# --------------------------------------------------------------------------------------------------
# Colour helpers
# --------------------------------------------------------------------------------------------------
def srgb_to_lab(rgb) -> tuple[float, float, float]:
    """sRGB (0..255, D65) -> CIE L*a*b*."""

    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(float(c)) for c in rgb)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e76(a, b) -> float:
    la, lb = srgb_to_lab(a), srgb_to_lab(b)
    return math.dist(la, lb)


# --------------------------------------------------------------------------------------------------
# Raster access and affine page model
# --------------------------------------------------------------------------------------------------
class _Raster:
    def __init__(self, image) -> None:
        self.rgb = image.convert("RGB")
        self.gray = image.convert("L")
        self.w, self.h = self.rgb.size
        self._g = self.gray.load()
        self._c = self.rgb.load()

    def g(self, x: float, y: float) -> float:
        """Bilinear gray at continuous pixel coordinates (pixel centres at +0.5)."""
        fx, fy = x - 0.5, y - 0.5
        x0, y0 = math.floor(fx), math.floor(fy)
        tx, ty = fx - x0, fy - y0
        w, h = self.w, self.h
        xs = (min(max(x0, 0), w - 1), min(max(x0 + 1, 0), w - 1))
        ys = (min(max(y0, 0), h - 1), min(max(y0 + 1, 0), h - 1))
        g = self._g
        return (g[xs[0], ys[0]] * (1 - tx) + g[xs[1], ys[0]] * tx) * (1 - ty) + (
            g[xs[0], ys[1]] * (1 - tx) + g[xs[1], ys[1]] * tx
        ) * ty

    def gi(self, x: int, y: int) -> int:
        return self._g[min(max(x, 0), self.w - 1), min(max(y, 0), self.h - 1)]

    def box_rgb(self, x0: float, y0: float, x1: float, y1: float) -> tuple[float, float, float]:
        """Mean RGB over a pixel box (continuous coordinates; at least one pixel)."""
        ix0, iy0 = max(0, int(math.floor(x0))), max(0, int(math.floor(y0)))
        ix1, iy1 = min(self.w, max(ix0 + 1, int(math.ceil(x1)))), min(self.h, max(iy0 + 1, int(math.ceil(y1))))
        n = max(1, (ix1 - ix0) * (iy1 - iy0))
        c = self._c
        sr = sg = sb = 0
        for y in range(iy0, iy1):
            for x in range(ix0, ix1):
                p = c[x, y]
                sr += p[0]
                sg += p[1]
                sb += p[2]
        return sr / n, sg / n, sb / n


class _Affine:
    """px = M . mm + t, fitted from point pairs."""

    def __init__(self, a: float, b: float, c: float, d: float, e: float, f: float) -> None:
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f

    def __call__(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.b * y + self.c, self.d * x + self.e * y + self.f

    def lin(self, x: float, y: float) -> tuple[float, float]:
        return self.a * x + self.b * y, self.d * x + self.e * y

    def inverse(self, u: float, v: float) -> tuple[float, float]:
        det = self.a * self.e - self.b * self.d
        du, dv = u - self.c, v - self.f
        return (self.e * du - self.b * dv) / det, (-self.d * du + self.a * dv) / det

    @staticmethod
    def fit(mm: list[tuple[float, float]], px: list[tuple[float, float]]) -> "_Affine":
        def solve(vals: list[float]) -> tuple[float, float, float]:
            m = [[0.0] * 3 for _ in range(3)]
            r = [0.0] * 3
            for (x, y), v in zip(mm, vals):
                row = (x, y, 1.0)
                for i in range(3):
                    r[i] += row[i] * v
                    for j in range(3):
                        m[i][j] += row[i] * row[j]
            for i in range(3):
                piv = max(range(i, 3), key=lambda k: abs(m[k][i]))
                m[i], m[piv] = m[piv], m[i]
                r[i], r[piv] = r[piv], r[i]
                for k in range(i + 1, 3):
                    q = m[k][i] / m[i][i]
                    for j in range(i, 3):
                        m[k][j] -= q * m[i][j]
                    r[k] -= q * r[i]
            out = [0.0] * 3
            for i in (2, 1, 0):
                out[i] = (r[i] - sum(m[i][j] * out[j] for j in range(i + 1, 3))) / m[i][i]
            return out[0], out[1], out[2]

        a, b, c = solve([p[0] for p in px])
        d, e, f = solve([p[1] for p in px])
        return _Affine(a, b, c, d, e, f)

    def decompose(self) -> dict[str, float]:
        sx = math.hypot(self.a, self.d)
        sy = math.hypot(self.b, self.e)
        cosphi = (self.a * self.b + self.d * self.e) / (sx * sy)
        return {
            "px_per_mm_x": sx,
            "px_per_mm_y": sy,
            "rotation_deg": math.degrees(math.atan2(self.d, self.a)),
            "shear": cosphi,
            "skew_deg": math.degrees(math.asin(max(-1.0, min(1.0, cosphi)))),
        }


def _centroid_1d(vals: list[float], x0: float) -> float | None:
    """Weighted centroid of positive weights (sub-pixel position of a dark peak in a darkness profile)."""
    s = sum(vals)
    if s <= 1e-9:
        return None
    return x0 + sum((i + 0.5) * v for i, v in enumerate(vals)) / s


def _peak_weights(vals: list[float]) -> list[float]:
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-6:
        return [0.0] * len(vals)
    thr = lo + 0.5 * (hi - lo)
    return [max(0.0, v - thr) for v in vals]


def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least squares y = m x + q; returns (m, q, rms residual)."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, my, 0.0
    m = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    q = my - m * mx
    rms = math.sqrt(sum((y - (m * x + q)) ** 2 for x, y in zip(xs, ys)) / n)
    return m, q, rms


def _mean(v) -> float:
    v = list(v)
    return sum(v) / len(v) if v else 0.0


# --------------------------------------------------------------------------------------------------
# Registration (page affine fit from the four registration crosses)
# --------------------------------------------------------------------------------------------------
def _ring_pts(r: float, n: int) -> list[tuple[float, float]]:
    return [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _locate_cross(ras: _Raster, A: _Affine, cx: float, cy: float, ring: float, arm: float, search: float = 10.0):
    dark = _ring_pts(ring, 24)
    for d in (0.8, 1.6, 2.4, 3.2):
        dark += [(d, 0), (-d, 0), (0, d), (0, -d)]
    inner = [(1.9 * math.cos(a), 1.9 * math.sin(a)) for a in (math.pi / 4 + k * math.pi / 2 for k in range(4))]
    inner += [(1.2 * math.cos(a), 1.2 * math.sin(a)) for a in (math.pi / 4 + k * math.pi / 2 for k in range(4))]
    outer = _ring_pts(ring + 1.5, 24)
    best, bs = (0.0, 0.0), -1e9
    steps = int(search / 0.5)

    def mean_at(pts, u0, v0):
        t = 0.0
        for dx, dy in pts:
            du, dv = A.lin(dx, dy)
            t += ras.gi(int(u0 + du), int(v0 + dv))
        return t / len(pts)

    for iy in range(-steps, steps + 1):
        for ix in range(-steps, steps + 1):
            ox, oy = cx + ix * 0.5, cy + iy * 0.5
            u0, v0 = A(ox, oy)
            # the ring and arms must be darker than BOTH the inside of the ring and the margin outside it
            score = min(mean_at(inner, u0, v0), mean_at(outer, u0, v0)) - mean_at(dark, u0, v0)
            if score > bs:
                bs, best = score, (ox, oy)
    # refine: darkness centroid of the whole mark inside a window around the coarse peak
    for _ in range(2):
        u0, v0 = A(*best)
        half = (arm + 0.4)
        hx, hy = abs(A.a) * half + abs(A.b) * half, abs(A.d) * half + abs(A.e) * half
        x0, x1 = int(u0 - hx), int(u0 + hx) + 1
        y0, y1 = int(v0 - hy), int(v0 + hy) + 1
        vals = [ras.gi(x, y) for y in range(y0, y1) for x in range(x0, x1)]
        bg = sorted(vals)[int(0.85 * (len(vals) - 1))]
        thr = bg - 0.08 * (bg - min(vals))
        sw = sx = sy = 0.0
        for y in range(y0, y1):
            for x in range(x0, x1):
                wgt = max(0.0, thr - ras.gi(x, y))
                sw += wgt
                sx += wgt * (x + 0.5)
                sy += wgt * (y + 0.5)
        if sw <= 0:
            return None, bs
        cu, cv = sx / sw, sy / sw
        best = A.inverse(cu, cv)
    return A(*best), bs


def _fit_page(ras: _Raster, layout: dict[str, Any], init: _Affine | None) -> tuple[_Affine | None, dict[str, Any]]:
    els = elements_by_id(layout)
    regs = [els[i] for i in ("reg_bl", "reg_br", "reg_tr", "reg_centre") if i in els]
    rw, rh = layout["raster_mm"]
    A = init or _Affine(ras.w / rw, 0.0, 0.0, 0.0, ras.h / rh, 0.0)
    mm_pts: list[tuple[float, float]] = []
    px_pts: list[tuple[float, float]] = []
    info: dict[str, Any] = {"registration": {}}
    for _ in range(2):  # the second pass searches around the refined model
        mm_pts, px_pts = [], []
        for e in regs:
            cx, cy = e["expected"]["centre_mm"]
            got, score = _locate_cross(ras, A, cx, cy, e["expected"]["ring_radius_mm"], e["expected"]["arm_mm"])
            if got is None:
                continue
            mm_pts.append((cx, cy))
            px_pts.append(got)
            info["registration"][e["id"]] = {"expected_mm": [cx, cy], "found_px": [got[0], got[1]], "score": score}
        if len(mm_pts) < 3:
            break
        A = _Affine.fit(mm_pts, px_pts)
    if len(mm_pts) < 3:
        return None, {"fit": "failed", "registration": info["registration"], "crosses_found": len(mm_pts)}
    res = [math.dist(A(*m), p) for m, p in zip(mm_pts, px_pts)]
    info["crosses_found"] = len(mm_pts)
    info["fit_residual_px_rms"] = math.sqrt(_mean(r * r for r in res))
    return A, info


# --------------------------------------------------------------------------------------------------
# Element analyzers
# --------------------------------------------------------------------------------------------------
THR_EDGE_CONTRAST = 35.0


def _edge_profile(ras: _Raster, A: _Affine, axis: str, pos_mm: float, span: tuple[float, float], half_mm: float, n: int = 12):
    """Average darkness profile perpendicular to a frame edge; returns (found, pos_mm_measured, contrast, expected_px)."""
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    k = sx if axis == "x" else sy
    half_px = max(2, int(round(half_mm * k)))
    acc = [0.0] * (2 * half_px + 1)
    cnt = 0
    exp_pos = []
    for i in range(n):
        t = span[0] + (span[1] - span[0]) * (i + 0.5) / n
        u, v = A(pos_mm, t) if axis == "x" else A(t, pos_mm)
        c = int(round(u if axis == "x" else v))
        exp_pos.append(c)
        for j in range(-half_px, half_px + 1):
            val = ras.gi(c + j, int(round(v))) if axis == "x" else ras.gi(int(round(u)), c + j)
            acc[j + half_px] += val
        cnt += 1
    prof = [a / cnt for a in acc]
    bg = sorted(prof)[len(prof) * 3 // 4]
    dark = [max(0.0, bg - p) for p in prof]
    contrast = max(dark)
    found = contrast >= THR_EDGE_CONTRAST
    c0 = sum(exp_pos) / len(exp_pos)
    if not found:
        return False, None, contrast, c0
    w = [max(0.0, d - 0.5 * contrast) for d in dark]
    cen = _centroid_1d(w, c0 - half_px)
    return True, cen, contrast, c0


def _analyze_frame(ras: _Raster, A: _Affine, el: dict[str, Any], nominal: float, layout_raster_mm: tuple[float, float]) -> dict[str, Any]:
    ex = el["expected"]
    out: dict[str, Any] = {"edges": {}}
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    clipped = []
    for ed in ex["edges"]:
        side, axis, pos, span = ed["side"], ed["axis"], ed["pos_mm"], tuple(ed["span_mm"])
        if el["anchor"] == "raster":
            # physical sheet edge: outermost pixel rows / columns of the raster
            n = 40
            fracs = []
            for depth in range(0, 8):
                dark = 0
                for i in range(n):
                    t = span[0] + (span[1] - span[0]) * (i + 0.5) / n
                    if axis == "x":
                        x = depth if side == "left" else ras.w - 1 - depth
                        y = int(A(0, t)[1])
                        g = ras.gi(x, y)
                    else:
                        y = depth if side == "top" else ras.h - 1 - depth
                        x = int(A(t, 0)[0])
                        g = ras.gi(x, y)
                    dark += g < 110
                fracs.append(dark / n)
            first = next((d for d, f in enumerate(fracs) if f >= 0.6), None)
            found = first is not None
            out["edges"][side] = {"found": found, "inset_px": first, "inset_mm": None if first is None else first / (sx if axis == "x" else sy)}
        else:
            found, cen, contrast, c0 = _edge_profile(ras, A, axis, pos, span, 1.6 if axis == "x" else 0.9)
            k = sx if axis == "x" else sy
            e: dict[str, Any] = {"found": found, "contrast": contrast}
            if found and cen is not None:
                size = ras.w if axis == "x" else ras.h
                edge_px = cen
                mid = span[0] + (span[1] - span[0]) / 2
                predicted = A(pos, mid)[0] if axis == "x" else A(mid, pos)[1]
                e["pos_px"] = edge_px
                e["offset_mm_vs_fit"] = (edge_px - predicted) / k
                e["offset_mm_vs_nominal"] = (edge_px - c0) / nominal
                e["margin_px"] = edge_px if side in ("left", "top") else size - edge_px
                e["margin_mm"] = e["margin_px"] / k
                rw, rh = layout_raster_mm
                e["expected_margin_mm"] = {"left": pos, "top": pos, "right": rw - pos, "bottom": rh - pos}[side]
                e["margin_error_mm"] = e["margin_mm"] - e["expected_margin_mm"]
            out["edges"][side] = e
        if not found:
            clipped.append(side)
    out["clipped_edges"] = clipped
    out["intact"] = not clipped
    return out


def _gray_stats(vals: list[float]) -> tuple[float, float]:
    s = sorted(vals)
    n = len(s)
    return s[int(0.05 * (n - 1))], s[int(0.95 * (n - 1))]


def _analyze_samples(ras: _Raster, A: _Affine, el: dict[str, Any], hw_mm: float = 0.35, hh_mm: float = 0.35) -> dict[str, Any]:
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    rows = []
    for s in el["samples"]:
        u, v = A(s["x_mm"], s["y_mm"])
        hw, hh = max(0.5, hw_mm * sx), max(0.5, hh_mm * sy)
        m = ras.box_rgb(u - hw, v - hh, u + hw, v + hh)
        exp = s["rgb"]
        err = [m[i] - exp[i] for i in range(3)]
        rows.append({"expected": exp, "measured": [round(c, 2) for c in m], "err": err, "delta_e": delta_e76(m, exp), "tol": s["tol"]})
    errs = [max(abs(c) for c in r["err"]) for r in rows]
    des = [r["delta_e"] for r in rows]
    out = {
        "n": len(rows),
        "rgb_err_max": max(errs) if errs else 0.0,
        "rgb_err_mean": _mean(errs),
        "delta_e_mean": _mean(des),
        "delta_e_max": max(des) if des else 0.0,
        "channel_bias": [_mean(r["err"][c] for r in rows) for c in range(3)],
        "points": rows,
    }
    return out


def _tick_regress(ras: _Raster, A: _Affine, horizontal: bool, ticks_mm: list[float], band: tuple[float, float], fixed_mm: float, nominal: float, tick_w_px: float) -> dict[str, Any]:
    """Locate ticks along an axis (band = perpendicular mm range) and regress position against mm."""
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    k_along = sx if horizontal else sy
    k_perp = sy if horizontal else sx
    pitch_px = (ticks_mm[1] - ticks_mm[0]) * k_along if len(ticks_mm) > 1 else 4.0
    half = max(1.6, 0.45 * pitch_px)
    mids = (band[0] + band[1]) / 2.0
    meas: list[float] = []
    exp: list[float] = []
    for t in ticks_mm:
        u, v = A(t, mids) if horizontal else A(mids, t)
        c_along = u if horizontal else v
        c_perp = v if horizontal else u
        hp = max(0.5, (band[1] - band[0]) / 2.0 * k_perp)
        lo, hi = int(round(c_along - half)), int(round(c_along + half))
        prof = []
        for p in range(lo, hi + 1):
            s = 0.0
            n = 0
            for q in range(int(round(c_perp - hp)), int(round(c_perp + hp)) + 1):
                s += 255 - (ras.gi(p, q) if horizontal else ras.gi(q, p))
                n += 1
            prof.append(s / max(1, n))
        w = _peak_weights(prof)
        cen = _centroid_1d(w, lo)
        if cen is None or max(prof) - min(prof) < 25:
            continue
        meas.append(cen)
        exp.append(t)
    n = len(exp)
    out: dict[str, Any] = {"ticks_found": n, "ticks_expected": len(ticks_mm)}
    if n < 4:
        out["ok"] = False
        return out
    m, q, rms = _linfit(exp, meas)
    out.update({
        "ok": True,
        "px_per_mm": m,
        "dpi": m * 25.4,
        "scale_vs_nominal": m / nominal,
        "scale_error_pct": (m / nominal - 1.0) * 100.0,
        "residual_px_rms": rms,
        "length_mm_measured": (meas[-1] - meas[0]) / nominal,
        "length_mm_expected": exp[-1] - exp[0],
    })
    out["length_error_mm"] = out["length_mm_measured"] - out["length_mm_expected"]
    out["length_error_pct"] = out["length_error_mm"] / out["length_mm_expected"] * 100.0
    return out


def _analyze_ruler(ras: _Raster, A: _Affine, el: dict[str, Any], nominal: float) -> dict[str, Any]:
    ex = el["expected"]
    horizontal = ex["axis"] == "x"
    pos_side = ex["ticks_point"].startswith("+")
    b = ex["baseline_mm"]
    short = ex["tick_len_mm"][0]
    if pos_side:
        band = (b + 0.35, b + max(0.5, short - 0.2))
    else:
        band = (b - max(0.5, short - 0.2), b - 0.35)
    r = _tick_regress(ras, A, horizontal, ex["ticks_mm"], band, b, nominal, ex["tick_width_px"])
    r["unit"] = ex["unit"]
    r["axis"] = ex["axis"]
    return r


def _edge_crossing(prof: list[float], x0: float, lo: float, hi: float, rising: bool) -> float | None:
    mid = 0.5 * (lo + hi)
    for i in range(len(prof) - 1):
        a, b = prof[i], prof[i + 1]
        if (a - mid) * (b - mid) <= 0 and a != b and ((b > a) == rising):
            return x0 + i + 0.5 + (mid - a) / (b - a)
    return None


def _analyze_measure_bar(ras: _Raster, A: _Affine, el: dict[str, Any], nominal: float) -> dict[str, Any]:
    ex = el["expected"]
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    ymid = (el["rect_mm"][1] + el["rect_mm"][3]) / 2.0
    xs = {}
    for name, x_mm, rising in (("left", ex["x0_mm"], False), ("right", ex["x1_mm"], True)):
        u, v = A(x_mm, ymid)
        half = int(round(2.5 * sx))
        lo_x = int(round(u)) - half
        prof = []
        for x in range(lo_x, int(round(u)) + half + 1):
            vals = [ras.gi(x, int(round(v)) + dy) for dy in (-1, 0, 1)] if sy < 6 else [ras.gi(x, int(round(v)) + dy) for dy in range(-2, 3)]
            prof.append(sum(vals) / len(vals))
        # ink is inside the bar: transition paper->ink at the left end (falling), ink->paper at the right end (rising)
        xs[name] = _edge_crossing(prof, lo_x, min(prof), max(prof), rising)
    out: dict[str, Any] = {"length_mm_expected": ex["length_mm"]}
    if xs["left"] is None or xs["right"] is None:
        out["ok"] = False
        return out
    length_px = xs["right"] - xs["left"]
    out.update({
        "ok": True,
        "length_px": length_px,
        "length_mm_measured_nominal": length_px / nominal,
        "length_mm_measured_fit": length_px / sx,
    })
    out["length_error_mm"] = out["length_mm_measured_nominal"] - ex["length_mm"]
    out["length_error_pct"] = out["length_error_mm"] / ex["length_mm"] * 100.0
    return out


def _analyze_hue_wheel(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    rows = _analyze_samples(ras, A, el, 0.5, 0.5)
    errs = []
    sats = []
    skipped = 0
    for i, p in enumerate(rows["points"]):
        hexp = i * 10.0
        r, g, b = (c / 255.0 for c in p["measured"])
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s < 0.12 or v < 0.2:
            skipped += 1
            continue
        d = (h * 360.0 - hexp + 180.0) % 360.0 - 180.0
        errs.append(d)
        sats.append(s - 0.8)
    out = {
        "n": len(rows["points"]),
        "hue_err_deg_mean_signed": _mean(errs),
        "hue_err_deg_max_abs": max((abs(e) for e in errs), default=None),
        "hue_err_deg_rms": math.sqrt(_mean(e * e for e in errs)) if errs else None,
        "saturation_err_mean": _mean(sats),
        "skipped_low_saturation": skipped,
        "delta_e_mean": rows["delta_e_mean"],
    }
    return out


def _goertzel_amp(vals: list[float], f_cyc_per_sample: float) -> float:
    n = len(vals)
    mu = sum(vals) / n
    re = im = 0.0
    for i, v in enumerate(vals):
        ang = 2 * math.pi * f_cyc_per_sample * (i + 0.5)
        re += (v - mu) * math.cos(ang)
        im += (v - mu) * math.sin(ang)
    return 2.0 * math.hypot(re, im) / n


IDEAL_FUND = (4.0 / math.pi) * 127.5  # fundamental of a 0 / 255 square wave
THR_RESOLVED = 0.25


def _analyze_line_pairs(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    ex = el["expected"]
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    vertical_lines = ex["orientation"].startswith("vertical")
    x0, y0, x1, y1 = el["rect_mm"]
    patch = ex["patch_mm"]
    rel = []
    for i, period in enumerate(ex["periods_mm"]):
        px0 = x0 + i * patch
        if vertical_lines:
            a0, a1, b0, b1 = px0 + 0.4, px0 + patch - 0.4, y0 + 0.5, y1 - 0.5
        else:
            a0, a1, b0, b1 = y0 + 0.4, y1 - 0.4, px0 + 0.5, px0 + patch - 0.5
        k_along, k_perp = (sx, sy) if vertical_lines else (sy, sx)
        # sample the patch on the pixel grid: average across the lines, profile along the period axis
        ua, va = A(a0, b0) if vertical_lines else A(b0, a0)
        ub, vb = A(a1, b1) if vertical_lines else A(b1, a1)
        if vertical_lines:
            xa, xb = int(math.ceil(min(ua, ub))), int(min(ua, ub) + abs(ub - ua))
            ya, yb = int(math.ceil(min(va, vb))), int(max(va, vb))
            prof = [_mean(ras.gi(x, y) for y in range(ya, yb + 1)) for x in range(xa, xb + 1)]
            f = 1.0 / (period * sx)
        else:
            ya, yb = int(math.ceil(min(va, vb))), int(max(va, vb))
            xa, xb = int(math.ceil(min(ua, ub))), int(max(ua, ub))
            prof = [_mean(ras.gi(x, y) for x in range(xa, xb + 1)) for y in range(ya, yb + 1)]
            f = 1.0 / (period * sy)
        if len(prof) < 4:
            rel.append(0.0)
            continue
        # phase-free amplitude at the nominal frequency; search a little around it for scale error
        best = max(_goertzel_amp(prof, f * (1.0 + d)) for d in (-0.04, -0.02, 0.0, 0.02, 0.04))
        rel.append(best / IDEAL_FUND)
    last = None
    for p, r in zip(ex["periods_mm"], rel):
        if r >= THR_RESOLVED:
            last = p
        else:
            break
    return {
        "periods_mm": ex["periods_mm"],
        "modulation_rel": rel,
        "threshold": THR_RESOLVED,
        "last_resolvable_period_mm": last,
        "limit_lp_per_mm": None if last is None else 1.0 / last,
        "axis": "x" if vertical_lines else "y",
    }


def _analyze_pixel_lines(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    x0, y0, x1, y1 = el["rect_mm"]
    ps = el["expected"]["patch_mm"]
    out = []
    for i, name in enumerate(el["expected"]["patches"]):
        u0, v0 = A(x0 + i * ps + 0.6, y0 + 0.6)
        u1, v1 = A(x0 + (i + 1) * ps - 0.6, y1 - 0.6)
        xa, xb, ya, yb = int(math.ceil(u0)), int(u1), int(math.ceil(v0)), int(v1)
        vertical = "vertical" in name
        if vertical:
            prof = [_mean(ras.gi(x, y) for y in range(ya, yb + 1)) for x in range(xa, xb + 1)]
        else:
            prof = [_mean(ras.gi(x, y) for x in range(xa, xb + 1)) for y in range(ya, yb + 1)]
        lo, hi = _gray_stats(prof) if len(prof) > 2 else (0, 0)
        out.append({"patch": name, "contrast": (hi - lo) / 255.0})
    return {"patches": out}


def _analyze_star(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    ex = el["expected"]
    cx, cy = ex["centre_mm"]
    cycles = ex["cycles"]
    rel = []
    radii = [ex["radius_mm"] - 0.6 - i * 0.75 for i in range(int((ex["radius_mm"] - 0.6) / 0.75))]
    radii = [r for r in radii if r > 0.5]
    for r in radii:
        n = 360
        vals = []
        for i in range(n):
            a = 2 * math.pi * i / n
            u, v = A(cx + r * math.cos(a), cy + r * math.sin(a))
            vals.append(ras.g(u, v))
        rel.append(_goertzel_amp(vals, cycles / n) / IDEAL_FUND)
    cutoff = None
    for r, q in zip(radii, rel):
        if q < THR_RESOLVED:
            cutoff = r
            break
    return {
        "radii_mm": radii,
        "modulation_rel": rel,
        "cutoff_radius_mm": cutoff,
        "limit_lp_per_mm": None if cutoff is None else cycles / (2 * math.pi * cutoff),
    }


def _analyze_slant(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    ex = el["expected"]
    cx, cy = ex["centre_mm"]
    half = ex["half_side_mm"]
    ang = math.radians(ex["angle_deg"])
    sx, sy = math.hypot(A.a, A.d), math.hypot(A.b, A.e)
    # left edge: runs along u2 = (-sin a, cos a); the edge passes through c - half*u1
    u1 = (math.cos(ang), math.sin(ang))
    u2 = (-math.sin(ang), math.cos(ang))
    xs_edge: list[float] = []
    ys_edge: list[float] = []
    samples: list[tuple[float, float, float]] = []  # (y_px, x_px, value)
    rows = 24
    for i in range(rows):
        t = -0.7 * half + 1.4 * half * (i + 0.5) / rows
        px_mm = (cx - half * u1[0] + t * u2[0], cy - half * u1[1] + t * u2[1])
        u, v = A(*px_mm)
        yi = int(round(v))
        x_lo = int(round(u - 3.2 * sx))
        prof = [ras.gi(x, yi) for x in range(x_lo, int(round(u + 3.2 * sx)) + 1)]
        for j, val in enumerate(prof):
            samples.append((yi + 0.5, x_lo + j + 0.5, val))
        c = _edge_crossing(prof, x_lo, min(prof), max(prof), True)  # paper -> ink? ink is on the right of the left edge: falling
        if c is None:
            c = _edge_crossing(prof, x_lo, min(prof), max(prof), False)
        if c is not None:
            xs_edge.append(c)
            ys_edge.append(yi + 0.5)
    if len(xs_edge) < 8:
        return {"ok": False}
    m, q, rms = _linfit(ys_edge, xs_edge)
    # ESF: distance (px) perpendicular to the fitted line vs value
    norm = math.sqrt(1 + m * m)
    bins: dict[int, list[float]] = {}
    for yy, xx, val in samples:
        d = (xx - (m * yy + q)) / norm
        if -10 <= d <= 10:
            bins.setdefault(int(math.floor(d * 4)), []).append(val)
    keys = sorted(bins)
    esf = [(k / 4.0 + 0.125, _mean(bins[k])) for k in keys]
    lo = _mean(v for _, v in esf[:6])
    hi = _mean(v for _, v in esf[-6:])
    span = hi - lo
    rise = None
    if abs(span) > 20:
        norm_esf = [(d, (v - lo) / span) for d, v in esf]
        d10 = d90 = None
        for (da, va), (db, vb) in zip(norm_esf, norm_esf[1:]):
            if d10 is None and (va - 0.1) * (vb - 0.1) <= 0 and va != vb:
                d10 = da + (0.1 - va) / (vb - va) * (db - da)
            if d90 is None and (va - 0.9) * (vb - 0.9) <= 0 and va != vb:
                d90 = da + (0.9 - va) / (vb - va) * (db - da)
        if d10 is not None and d90 is not None:
            rise = abs(d90 - d10)
    return {
        "ok": True,
        "edge_angle_deg": math.degrees(math.atan(m)),
        "expected_angle_deg_in_layout": -ex["angle_deg"],
        "line_residual_px": rms,
        "rise_10_90_px": rise,
        "rise_10_90_mm": None if rise is None else rise / sx,
    }


def _refine_along(ras: _Raster, A: _Affine, p_mm: tuple[float, float], dir_mm: tuple[float, float], search: float) -> tuple[float, float] | None:
    """Darkest-line centroid along a direction (mm) around an expected point; returns the px point."""
    n = int(search / 0.05)
    vals = []
    for i in range(-n, n + 1):
        r = i * 0.05
        u, v = A(p_mm[0] + dir_mm[0] * r, p_mm[1] + dir_mm[1] * r)
        vals.append(255 - ras.g(u, v))
    w = _peak_weights(vals)
    if sum(w) <= 0 or max(vals) - min(vals) < 30:
        return None
    cen = sum(wi * (i - n) * 0.05 for i, wi in enumerate(w)) / sum(w)
    return A(p_mm[0] + dir_mm[0] * cen, p_mm[1] + dir_mm[1] * cen)


def _ellipse_from_points(pts_px: list[tuple[float, float]], ideal_mm: list[tuple[float, float]]) -> dict[str, Any]:
    """M M^T from point covariances: cov(px) = M cov(mm) M^T with cov(mm) = c I for a symmetric ring."""
    n = len(pts_px)
    mx, my = _mean(p[0] for p in pts_px), _mean(p[1] for p in pts_px)
    cxx = _mean((p[0] - mx) ** 2 for p in pts_px)
    cyy = _mean((p[1] - my) ** 2 for p in pts_px)
    cxy = _mean((p[0] - mx) * (p[1] - my) for p in pts_px)
    imx, imy = _mean(p[0] for p in ideal_mm), _mean(p[1] for p in ideal_mm)
    c = _mean((p[0] - imx) ** 2 + (p[1] - imy) ** 2 for p in ideal_mm) / 2.0
    tr, det = cxx + cyy, cxx * cyy - cxy * cxy
    disc = math.sqrt(max(0.0, tr * tr / 4 - det))
    l1, l2 = tr / 2 + disc, max(1e-12, tr / 2 - disc)
    ang = 0.5 * math.degrees(math.atan2(2 * cxy, cxx - cyy))
    return {
        "n": n,
        "axis_ratio": math.sqrt(l1 / l2),
        "aspect_error_pct": (math.sqrt(l1 / l2) - 1.0) * 100.0,
        "major_axis_angle_deg": ang,
        "px_per_mm_rows_x": math.sqrt(cxx / c),
        "px_per_mm_rows_y": math.sqrt(cyy / c),
        "x_over_y": math.sqrt(cxx / cyy),
        "shear_cov": cxy / math.sqrt(cxx * cyy),
    }


def _analyze_circles(ras: _Raster, A: _Affine, centre: tuple[float, float], radii: list[float], n: int = 72) -> dict[str, Any]:
    out = []
    best = None
    for r in radii:
        pts_px, ideal = [], []
        for i in range(n):
            a = 2 * math.pi * i / n
            d = (math.cos(a), math.sin(a))
            p = (centre[0] + r * d[0], centre[1] + r * d[1])
            q = _refine_along(ras, A, p, d, 1.0)
            if q is None:
                continue
            pts_px.append(q)
            ideal.append(p)
        if len(pts_px) < n * 0.8:
            out.append({"radius_mm": r, "ok": False, "found": len(pts_px)})
            continue
        # uniform parameter sampling is required: drop the fit if rays were lost unevenly
        rep = _ellipse_from_points(pts_px, ideal)
        rep.update({"radius_mm": r, "ok": True, "found": len(pts_px)})
        out.append(rep)
        best = rep
    return {"circles": out, "largest": best}


def _analyze_hexagon(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    ex = el["expected"]
    cx, cy = ex["centre_mm"]
    R, ap = ex["circumradius_mm"], ex["apothem_mm"]
    pts_px, ideal = [], []
    for i in range(12):
        a = math.radians(30.0 * i)
        r = R if i % 2 == 0 else ap
        d = (math.cos(a), math.sin(a))
        p = (cx + r * d[0], cy + r * d[1])
        q = _refine_along(ras, A, p, d, 0.8)
        if q is not None:
            pts_px.append(q)
            ideal.append(p)
    if len(pts_px) < 12:
        return {"ok": False, "found": len(pts_px)}
    rep = _ellipse_from_points(pts_px, ideal)
    rep["ok"] = True
    return rep


def _analyze_square_circle(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    ex = el["expected"]
    rep = _analyze_circles(ras, A, tuple(ex["centre_mm"]), [ex["radius_mm"]])
    # side lengths of the square from its four edge midpoints
    x0, y0, x1, y1 = ex["square_mm"]
    out = {"circle": rep["largest"]}
    return out


def _analyze_markers(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    ex = el["expected"]
    vx, vy = ex["vertex_mm"]
    arm = ex["arm_mm"]
    dirs = []
    for u in (ex["u1"], ex["u2"]):
        perp = (-u[1], u[0])
        pts = []
        for t in (0.35, 0.8, 1.25, 1.7, 2.05):
            p = (vx + u[0] * t * arm / 2.4, vy + u[1] * t * arm / 2.4)
            q = _refine_along(ras, A, p, perp, 0.45)
            if q is not None:
                pts.append(q)
        if len(pts) < 4:
            return {"ok": False}
        # direction by PCA of the points
        mx, my = _mean(p[0] for p in pts), _mean(p[1] for p in pts)
        cxx = _mean((p[0] - mx) ** 2 for p in pts)
        cyy = _mean((p[1] - my) ** 2 for p in pts)
        cxy = _mean((p[0] - mx) * (p[1] - my) for p in pts)
        ang = 0.5 * math.atan2(2 * cxy, cxx - cyy)
        dirs.append((math.cos(ang), math.sin(ang)))
    dot = dirs[0][0] * dirs[1][0] + dirs[0][1] * dirs[1][1]
    angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(dot)))))
    return {"ok": True, "arm_angle_deg": angle, "expected_deg": 90.0, "error_deg": angle - 90.0}


def _line_positions(ras: _Raster, A: _Affine, items: list[tuple[float, tuple[float, float]]], vertical: bool, nominal: float) -> list[tuple[float, float]]:
    """(expected mm, measured px) of axis-aligned lines: items = [(pos_mm, (span0, span1))]."""
    out = []
    for pos, (s0, s1) in items:
        a, b = s0 + 0.2 * (s1 - s0), s1 - 0.2 * (s1 - s0)
        # expected centre px of the line and the sampling extent along the line
        (u0, v0), (u1, v1) = (A(pos, a), A(pos, b)) if vertical else (A(a, pos), A(b, pos))
        half = max(2, int(round(0.7 * nominal)))
        prof = []
        if vertical:
            c = int(round((u0 + u1) / 2))
            ya, yb = int(min(v0, v1)), int(max(v0, v1))
            if yb - ya < 2:
                continue
            prof = [255 - _mean(ras.gi(x, y) for y in range(ya, yb + 1)) for x in range(c - half, c + half + 1)]
            w = _peak_weights(prof)
            cen = _centroid_1d(w, c - half)
        else:
            c = int(round((v0 + v1) / 2))
            xa, xb = int(min(u0, u1)), int(max(u0, u1))
            if xb - xa < 2:
                continue
            prof = [255 - _mean(ras.gi(x, y) for x in range(xa, xb + 1)) for y in range(c - half, c + half + 1)]
            w = _peak_weights(prof)
            cen = _centroid_1d(w, c - half)
        if cen is not None and max(prof) - min(prof) > 25:
            out.append((pos, cen))
    return out


def _analyze_golden(ras: _Raster, A: _Affine, el: dict[str, Any], nominal: float) -> dict[str, Any]:
    ex = el["expected"]
    vx = _line_positions(ras, A, [(l["x_mm"], tuple(l["y_span_mm"])) for l in ex["vertical_lines"]], True, nominal)
    hy = _line_positions(ras, A, [(l["y_mm"], tuple(l["x_span_mm"])) for l in ex["horizontal_lines"]], False, nominal)
    out: dict[str, Any] = {"vertical_lines_found": len(vx), "horizontal_lines_found": len(hy)}
    if len(vx) < 4 or len(hy) < 4:
        out["ok"] = False
        return out
    mx, qx, rx = _linfit([p[0] for p in vx], [p[1] for p in vx])
    my, qy, ry = _linfit([p[0] for p in hy], [p[1] for p in hy])
    wpx = max(p[1] for p in vx) - min(p[1] for p in vx)
    hpx = max(p[1] for p in hy) - min(p[1] for p in hy)
    out.update({
        "ok": True,
        "px_per_mm_x": mx,
        "px_per_mm_y": my,
        "scale_x_vs_nominal": mx / nominal,
        "scale_y_vs_nominal": my / nominal,
        "aspect_x_over_y": mx / my,
        "aspect_error_pct": (mx / my - 1.0) * 100.0,
        "residual_px_rms": [rx, ry],
        "width_over_height": wpx / hpx,
        "expected_width_over_height": ex["width_units"] / ex["height_units"],
        "phi_error_pct": ((wpx / hpx) / (ex["width_units"] / ex["height_units"]) - 1.0) * 100.0,
    })
    return out


# Sierpinski ----------------------------------------------------------------------------------------
def _sierp_ink(ex: dict[str, Any], x: float, y: float, depth: int | None = None) -> bool:
    side, x0, top = ex["side_mm"], ex["x0_mm"], ex["top_mm"]
    depth = ex["depth"] if depth is None else depth
    hgt = ex["height_mm"]
    t = (top + hgt - y) / hgt
    s = (x - x0) / side - 0.5 * t
    if s < 0 or t < 0 or s + t >= 1.0:
        return False
    n = float(1 << depth)
    fs, ft = s * n, t * n
    i, j = int(math.floor(fs)), int(math.floor(ft))
    return (i & j) == 0 and (fs - i) + (ft - j) < 1.0


def _inside_sierp(ex: dict[str, Any], x: float, y: float) -> bool:
    hgt = ex["height_mm"]
    t = (ex["top_mm"] + hgt - y) / hgt
    s = (x - ex["x0_mm"]) / ex["side_mm"] - 0.5 * t
    return s >= 0 and t >= 0 and s + t < 1.0


def _disc_mean(ras: _Raster, A: _Affine, p: tuple[float, float], r_mm: float) -> float:
    pts = [(0.0, 0.0)] + [(r_mm * math.cos(a * math.pi / 3), r_mm * math.sin(a * math.pi / 3)) for a in range(6)]
    vals = []
    for dx, dy in pts:
        u, v = A(p[0] + dx, p[1] + dy)
        vals.append(ras.g(u, v))
    return _mean(vals)


def _analyze_sierpinski(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    """Level-L contrast: scan value inside the level-L holes versus inside the solid ink of the depth-L ideal.

    Blur or a coarse sampling grid fills the small holes first, so the contrast falls level by level; the smallest
    resolved level is the deepest one (counted from 1) whose contrast is still at least 50 % of what the ideal
    page gives at the same sample points.
    """
    ex = el["expected"]
    depth = ex["depth"]
    contrasts = []
    for level in range(1, depth + 1):
        s = ex["side_mm"] / (1 << level)
        rho = 0.35 * s / (2 * math.sqrt(3))
        step = s / 3.0
        ring = [(rho * math.cos(a), rho * math.sin(a)) for a in (0.0, 2.0944, 4.1888)]
        holes: list[tuple[float, float]] = []
        inks: list[tuple[float, float]] = []
        x = ex["x0_mm"]
        while x < ex["x0_mm"] + ex["side_mm"]:
            y = ex["top_mm"]
            while y < ex["base_y_mm"]:
                if _inside_sierp(ex, x, y):
                    here = _sierp_ink(ex, x, y, level)
                    if all(_sierp_ink(ex, x + dx, y + dy, level) == here and _inside_sierp(ex, x + dx, y + dy) for dx, dy in ring):
                        (inks if here else holes).append((x, y))
                y += step
            x += step
        if not holes or not inks:
            contrasts.append(None)
            continue
        hsel = holes[:: max(1, len(holes) // 150)]
        isel = inks[:: max(1, len(inks) // 150)]
        hv = [_disc_mean(ras, A, c, rho / 2) for c in hsel]
        iv = [_disc_mean(ras, A, c, rho / 2) for c in isel]

        def ideal(c: tuple[float, float]) -> float:
            pts = [(0.0, 0.0)] + [(rho / 2 * math.cos(k * math.pi / 3), rho / 2 * math.sin(k * math.pi / 3)) for k in range(6)]
            return _mean(0.0 if _sierp_ink(ex, c[0] + dx, c[1] + dy) else 255.0 for dx, dy in pts)

        ideal_c = _mean(ideal(c) for c in hsel) - _mean(ideal(c) for c in isel)
        contrasts.append((_mean(hv) - _mean(iv)) / ideal_c if ideal_c > 5 else None)
    smallest = 0
    for i, c in enumerate(contrasts):
        if c is None:  # level 1 has no usable ink-only region at the sample radius
            continue
        if c >= 0.5:
            smallest = i + 1
        else:
            break
    return {
        "contrast_by_level": contrasts,
        "smallest_resolved_level": smallest,
        "smallest_resolved_hole_mm": ex["side_mm"] / (1 << smallest) if smallest else None,
        "levels": depth,
    }


def _analyze_text(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    x0, y0, x1, y1 = el["rect_mm"]
    (u0, v0), (u1, v1) = A(x0, y0), A(x1, y1)
    xa, xb, ya, yb = int(min(u0, u1)), int(math.ceil(max(u0, u1))), int(min(v0, v1)), int(math.ceil(max(v0, v1)))
    vals = [ras.gi(x, y) for y in range(ya, yb + 1) for x in range(xa, xb + 1)]
    lo, hi = _gray_stats(vals)
    dark = sum(1 for v in vals if v < 128) / len(vals)
    out = {"contrast": (hi - lo) / 255.0, "ink_fraction_measured": dark}
    exp = el["expected"].get("ink_fraction")
    if exp is not None:
        out["ink_fraction_expected"] = exp
        out["legible"] = (hi - lo) > 90 and 0.55 * exp <= dark <= 1.7 * exp
    return out


def _analyze_presence(ras: _Raster, A: _Affine, el: dict[str, Any]) -> dict[str, Any]:
    x0, y0, x1, y1 = el["rect_mm"]
    pts = [A(x0 + (x1 - x0) * i / 6.0, y0 + (y1 - y0) * j / 6.0) for i in range(7) for j in range(7)]
    vals = [ras.g(u, v) for u, v in pts if 1 <= u < ras.w - 1 and 1 <= v < ras.h - 1]
    return {"min_gray": min(vals) if vals else None, "present": bool(vals) and min(vals) < 140}


def _analyze_line_grid(ras: _Raster, A: _Affine, el: dict[str, Any], nominal: float) -> dict[str, Any]:
    x0, y0, x1, y1 = el["rect_mm"]
    x1, y1 = x1 - 0.4, y1 - 0.4
    vx = [x0 + i for i in range(int(round(x1 - x0)) + 1)]
    hy = [y0 + i for i in range(int(round(y1 - y0)) + 1)]
    rv = _tick_regress(ras, A, True, vx, (y0 + 2.0, y1 - 2.0), y0, nominal, 1)
    rh = _tick_regress(ras, A, False, hy, (x0 + 2.0, x1 - 2.0), x0, nominal, 1)
    return {"x": rv, "y": rh}


# --------------------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------------------
def analyze_diag_page(image, layout: dict[str, Any] | None = None, dpi: int | None = None, scan_dpi: float | None = None,
                      init: tuple[float, float, float, float, float, float] | None = None, detector: str = "auto",
                      marker_fallback: bool = True) -> dict[str, Any]:
    """Per-element report for a scanned / printed-back ``diag`` page (layout v3: ``fakescan.diag3``; v2 below).

    ``image``: PIL image of the page (any size).  ``layout``: from :func:`diag_page_layout` (default: built for
    ``dpi``, default 100).  ``scan_dpi``: the resolution the raster is *supposed* to have (default: the layout's
    effective dpi); every ``*_vs_nominal`` figure and mm error is relative to it.  ``init``: optional initial
    affine ``(a, b, c, d, e, f)`` (px = a*x + b*y + c, ...) for pages that are shifted or scaled a lot; the default
    assumes the raster shows the whole sheet.  ``detector`` (layout v3): ``"auto"`` uses ``cv2.aruco`` for the ArUco
    markers when ``opencv-contrib`` is importable and the pure-Python detector otherwise; ``"python"`` / ``"cv2"`` force one.
    The page fit uses the registration crosses; fewer than three real crosses (weak ones are rejected as clutter) fall
    back to the marker corners (``summary.fit == "markers"``) unless ``marker_fallback`` is false, and otherwise the
    report says ``fit: failed``.
    """
    lay = layout or diag_page_layout(dpi or 100)
    if lay["layout_version"] >= 3:
        from .diag3 import analyze_v3

        return analyze_v3(image, lay, scan_dpi=scan_dpi, init=init, detector=detector, marker_fallback=marker_fallback)
    ras = _Raster(image)
    if lay["layout_version"] < 2:
        raise ValueError("analyze_diag_page needs a layout-v2 layout")
    nominal = (scan_dpi or lay["dpi_effective"]) / 25.4
    A, geo = _fit_page(ras, lay, _Affine(*init) if init else None)
    report: dict[str, Any] = {
        "layout_version": lay["layout_version"],
        "colour_space": lay["colour_space"],
        "image_px": [ras.w, ras.h],
        "nominal_px_per_mm": nominal,
        "geometry": geo,
        "elements": {},
        "summary": {},
    }
    if A is None:
        report["summary"] = {"fit": "failed"}
        return report
    d = A.decompose()
    sx, sy = d["px_per_mm_x"], d["px_per_mm_y"]
    geo.update({
        **d,
        "dpi_x": sx * 25.4,
        "dpi_y": sy * 25.4,
        "scale_x_vs_nominal": sx / nominal,
        "scale_y_vs_nominal": sy / nominal,
        "aspect_x_over_y": sx / sy,
        "origin_px": [A.c, A.f],
        "origin_offset_mm": [A.c / nominal, A.f / nominal],
        "affine": [A.a, A.b, A.c, A.d, A.e, A.f],
    })
    els = elements_by_id(lay)
    out = report["elements"]
    clipped: list[str] = []
    for el in lay["elements"]:
        if not el.get("drawn", True):
            continue
        eid, kind = el["id"], el["kind"]
        try:
            if kind == "frame":
                r = _analyze_frame(ras, A, el, nominal, tuple(lay["raster_mm"]))
                clipped += [f"{eid}:{s}" for s in r["clipped_edges"]]
            elif kind == "ruler":
                r = _analyze_ruler(ras, A, el, nominal)
            elif kind == "measure_bar":
                r = _analyze_measure_bar(ras, A, el, nominal)
            elif kind == "hue_wheel":
                r = _analyze_hue_wheel(ras, A, el)
            elif kind == "ramp":
                r = _analyze_samples(ras, A, el, 0.3, 1.2)
            elif kind in ("step_strip", "clip_patch", "colour_patches", "grey_wedge"):
                r = _analyze_samples(ras, A, el, 0.6, 0.6)
            elif kind == "line_pairs":
                r = _analyze_line_pairs(ras, A, el)
            elif kind == "pixel_exact" and eid == "res_lines_px":
                r = _analyze_pixel_lines(ras, A, el)
            elif kind == "siemens_star":
                r = _analyze_star(ras, A, el)
            elif kind == "slanted_edge":
                r = _analyze_slant(ras, A, el)
            elif kind == "golden_construction":
                r = _analyze_golden(ras, A, el, nominal)
            elif kind == "sierpinski":
                r = _analyze_sierpinski(ras, A, el)
            elif kind == "concentric_circles":
                r = _analyze_circles(ras, A, tuple(el["expected"]["centre_mm"]), list(el["expected"]["radii_mm"]))
            elif kind == "circle_in_square":
                r = _analyze_square_circle(ras, A, el)
            elif kind == "regular_polygon":
                r = _analyze_hexagon(ras, A, el)
            elif kind == "right_angle_marker":
                r = _analyze_markers(ras, A, el)
            elif kind == "line_grid":
                r = _analyze_line_grid(ras, A, el, nominal)
            elif kind == "text":
                r = _analyze_text(ras, A, el)
            elif kind in ("marks", "registration_cross"):
                r = _analyze_presence(ras, A, el)
            else:
                continue
        except Exception as exc:  # one broken element must not hide the others
            r = {"error": f"{type(exc).__name__}: {exc}"}
        out[eid] = r

    # clip detection from the near-black / near-white strips
    summary: dict[str, Any] = {
        "scale_x": sx / nominal,
        "scale_y": sy / nominal,
        "aspect_x_over_y": sx / sy,
        "rotation_deg": d["rotation_deg"],
        "shear": d["shear"],
        "origin_offset_mm": [A.c / nominal, A.f / nominal],
        "clipped_edges": clipped,
    }
    for name, strip, clip, bad in (("shadows", "near_black", "clip_black", +1), ("highlights", "near_white", "clip_white", -1)):
        if strip in out and clip in out and out[strip].get("points") and out[clip].get("points"):
            ref = out[clip]["points"][0]["measured"][1]
            crushed = 0
            level = None
            for p in out[strip]["points"]:
                m = p["measured"][1]
                if (m - ref) * bad <= 0.5:
                    crushed += 1
                    level = p["expected"][1]
                else:
                    break
            summary[f"{name}_clipped_steps"] = crushed
            summary[f"{name}_clipped_up_to_level"] = level
    rl = [k for k in ("res_lines_v", "res_lines_h") if k in out]
    for k in rl:
        summary[f"{k}_last_resolvable_period_mm"] = out[k].get("last_resolvable_period_mm")
    if "sierpinski" in out:
        summary["sierpinski_smallest_resolved_level"] = out["sierpinski"]["smallest_resolved_level"]
    if "hue_wheel" in out:
        summary["hue_err_deg_max_abs"] = out["hue_wheel"].get("hue_err_deg_max_abs")
    for k in ("ruler_metric_h", "ruler_metric_v", "ruler_inch_h", "ruler_inch_v"):
        if out.get(k, {}).get("ok"):
            summary[f"{k}_dpi"] = out[k]["dpi"]
    if out.get("measure_bar", {}).get("ok"):
        summary["measure_bar_length_error_mm"] = out["measure_bar"]["length_error_mm"]
        summary["measure_bar_length_error_pct"] = out["measure_bar"]["length_error_pct"]
    report["summary"] = summary
    return report
