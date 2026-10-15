"""Analyzer for the ``diag`` page layout v3 (the parametric generator, see ``docs/diag-page-v3.md``).

Everything is driven by the layout JSON (``diag_page_layout(...)``): element rectangles, expected values and
sample points come from the generator for the sheet size, dpi, page / side that were asked for, so nothing here
is hard-coded to a paper size.  The page is fitted with an affine model from the four registration crosses, then
each drawn element gets a report.  Pure Python plus Pillow.

``analyze_v3(image, layout, scan_dpi=None, init=None)`` returns::

    {"layout_version": 3, "geometry": {...}, "elements": {id: {...}}, "summary": {...}}
"""

from __future__ import annotations

import math
from typing import Any

from . import diag as _d
from . import diag_markers as _m

__all__ = ["analyze_v3"]

# registration crosses: a cross whose score is below this share of the best one, or below this share of the
# image contrast, is clutter (a real cross scores about the page contrast; cut-off ones match random ink)
WEAK_CROSS_REL = 0.4
WEAK_CROSS_ABS = 0.25

_TRANSFORMS = ("identity", "flip_h", "flip_v", "rot180")


def _apply(rows: list[str], name: str) -> list[str]:
    if name == "flip_h":
        return [r[::-1] for r in rows]
    if name == "flip_v":
        return rows[::-1]
    if name == "rot180":
        return [r[::-1] for r in rows[::-1]]
    return rows


def _box(ras, A, x: float, y: float, half_mm: float):
    u, v = A(x, y)
    hu = (abs(A.a) + abs(A.b)) * half_mm
    hv = (abs(A.d) + abs(A.e)) * half_mm
    return ras.box_rgb(u - hu, v - hv, u + hu, v + hv)


def _lum(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _hue(rgb) -> float:
    import colorsys

    h, _, _ = colorsys.rgb_to_hsv(*(c / 255.0 for c in rgb))
    return h * 360.0


def _samples(ras, A, el, half_mm: float = 0.3) -> dict[str, Any]:
    errs, des, bias = [], [], [0.0, 0.0, 0.0]
    rows = []
    for s in el.get("samples", []):
        got = _box(ras, A, s["x_mm"], s["y_mm"], half_mm)
        want = s["rgb"]
        e = [got[i] - want[i] for i in range(3)]
        errs.append(max(abs(c) for c in e))
        des.append(_d.delta_e76(got, want))
        for i in range(3):
            bias[i] += e[i]
        rows.append({"got": [round(c, 1) for c in got], "want": want})
    n = max(1, len(errs))
    return {
        "n": len(errs),
        "max_abs_err": max(errs) if errs else None,
        "mean_delta_e76": sum(des) / n if des else None,
        "max_delta_e76": max(des) if des else None,
        "channel_bias": [b / n for b in bias],
        "samples": rows,
    }


# ------------------------------------------------------------------------------------------------ geometry
def _contrast(ras) -> float:
    """Dynamic range of the page: 99.5th minus 0.5th grey percentile."""
    h = ras.gray.histogram()
    tot = sum(h)
    lo = hi = None
    acc = 0
    for v, n in enumerate(h):
        acc += n
        if lo is None and acc >= 0.005 * tot:
            lo = v
        if hi is None and acc >= 0.995 * tot:
            hi = v
            break
    return float((hi or 255) - (lo or 0))


def _is_mono(ras) -> bool:
    """True when the image carries no colour (a grey / bilevel scan, or a ``colour=gray`` page)."""
    c = ras._c
    n = 0
    for y in range(0, ras.h, max(1, ras.h // 60)):
        for x in range(0, ras.w, max(1, ras.w // 60)):
            r, g, b = c[x, y][:3]
            if max(r, g, b) - min(r, g, b) > 6:
                n += 1
    return n == 0


def _fit(ras, lay, init, scan_dpi: float | None = None):
    """Affine fit from the registration crosses, rejecting clutter: a cross that scores below
    ``WEAK_CROSS_REL`` x the best score (or ``WEAK_CROSS_ABS`` x the page contrast) is not a cross; fewer than three
    real crosses is ``fit: failed``.  A fourth cross that disagrees with the other three is dropped.  The plausibility
    test (scale within 0.4x..2.5x, skew under 20 degrees) is relative to the *analysis* dpi: ``scan_dpi`` when given,
    else the layout's ``dpi_effective`` (a layout generated at 400 dpi and analysed on a 150 dpi raster needs
    ``scan_dpi=150``)."""
    els = _d.elements_by_id(lay)
    regs = [els[i] for i in ("reg_tl", "reg_tr", "reg_bl", "reg_br") if i in els and els[i].get("drawn", True)]
    rw, rh = lay["raster_mm"]
    A = init or _d._Affine(ras.w / rw, 0.0, 0.0, 0.0, ras.h / rh, 0.0)
    info: dict[str, Any] = {"registration": {}}
    contrast = _contrast(ras)
    accepted: list[tuple[str, tuple[float, float], tuple[float, float], float]] = []
    for _ in range(2):
        found = []
        info["registration"] = {}
        for e in regs:
            cx, cy = e["expected"]["centre_mm"]
            got, score = _d._locate_cross(ras, A, cx, cy, e["expected"]["ring_radius_mm"], e["expected"]["arm_mm"], 8.0)
            if got is None:
                info["registration"][e["id"]] = {"expected_mm": [cx, cy], "found_px": None, "score": score, "weak": True}
                continue
            found.append((e["id"], (cx, cy), got, score))
            info["registration"][e["id"]] = {"expected_mm": [cx, cy], "found_px": list(got), "score": score}
        best = max((f[3] for f in found), default=0.0)
        floor = max(WEAK_CROSS_REL * best, WEAK_CROSS_ABS * contrast)
        accepted = [f for f in found if f[3] >= floor]
        for f in found:
            info["registration"][f[0]]["weak"] = f[3] < floor
        info["cross_score_floor"] = floor
        if len(accepted) < 3:
            break
        A = _d._Affine.fit([f[1] for f in accepted], [f[2] for f in accepted])
    if len(accepted) < 3:
        info.update({"fit": "failed", "crosses_found": len(accepted), "reason": "fewer than three registration crosses"})
        return None, info
    # a fourth cross that does not fit the other three is not a cross either
    if len(accepted) == 4:
        res = [math.dist(A(*f[1]), f[2]) for f in accepted]
        rms = math.sqrt(_d._mean(r * r for r in res))
        if rms > max(1.5, 0.002 * max(ras.w, ras.h)):
            errs = []
            for k in range(4):
                rest = [f for i, f in enumerate(accepted) if i != k]
                Ak = _d._Affine.fit([f[1] for f in rest], [f[2] for f in rest])
                errs.append(math.dist(Ak(*accepted[k][1]), accepted[k][2]))
            worst = max(range(4), key=lambda k: errs[k])
            info["registration"][accepted[worst][0]]["weak"] = True
            info["dropped_cross"] = accepted[worst][0]
            accepted = [f for i, f in enumerate(accepted) if i != worst]
            A = _d._Affine.fit([f[1] for f in accepted], [f[2] for f in accepted])
    nominal = (scan_dpi or lay["dpi_effective"]) / 25.4
    dec = A.decompose()
    sx, sy = dec["px_per_mm_x"], dec["px_per_mm_y"]
    if not (0.4 < sx / nominal < 2.5 and 0.4 < sy / nominal < 2.5) or abs(dec["skew_deg"]) > 20.0:
        info.update({"fit": "failed", "crosses_found": len(accepted), "reason": "implausible affine from the crosses"})
        return None, info
    mm_pts = [f[1] for f in accepted]
    px_pts = [f[2] for f in accepted]
    res = [math.dist(A(*m), p) for m, p in zip(mm_pts, px_pts)]
    info["crosses_found"] = len(accepted)
    info["fit_residual_px_rms"] = math.sqrt(_d._mean(r * r for r in res))
    info["fit"] = "crosses"
    return A, info


# ------------------------------------------------------------------------------------------------ element analyzers
def _grid_cells(ras, A, rect, bm: list[str], ncols: int = 5, margin: float = 0.2):
    """Binary cell grid of a bitmap-sized rect: True = dark."""
    x0, y0, x1, y1 = rect
    nr = len(bm)
    cw, ch = (x1 - x0) / ncols, (y1 - y0) / nr
    vals = []
    for r in range(nr):
        row = []
        for c in range(ncols):
            row.append(_lum(_box(ras, A, x0 + (c + 0.5) * cw, y0 + (r + 0.5) * ch, min(cw, ch) * 0.25)))
        vals.append(row)
    flat = [v for row in vals for v in row]
    thr = (min(flat) + max(flat)) / 2.0
    return [[v < thr for v in row] for row in vals], max(flat) - min(flat)


def _match(cells, bm: list[str]) -> float:
    n = ok = 0
    for r, row in enumerate(bm):
        for c, ch in enumerate(row):
            n += 1
            ok += (ch == "1") == cells[r][c]
    return ok / n


def _analyze_orientation(ras, A, lay) -> dict[str, Any]:
    els = _d.elements_by_id(lay)
    corners = ["tl", "tr", "bl", "br"]
    glyph = {}
    for c in corners:
        e = els.get("orient_" + c)
        if not e:
            return {}
        glyph[c] = e
    obs = {}
    for c in corners:
        e = glyph[c]
        r = e["rect_mm"]
        cells, contrast = _grid_cells(ras, A, r, e["expected"]["bitmap"])
        obs[c] = (cells, contrast)
    # page transform T: observed corner c shows the glyph of corner m(c) transformed by T
    cmap = {
        "identity": {"tl": "tl", "tr": "tr", "bl": "bl", "br": "br"},
        "flip_h": {"tl": "tr", "tr": "tl", "bl": "br", "br": "bl"},
        "flip_v": {"tl": "bl", "tr": "br", "bl": "tl", "br": "tr"},
        "rot180": {"tl": "br", "tr": "bl", "bl": "tr", "br": "tl"},
    }
    scores = {}
    for t in _TRANSFORMS:
        tot = 0.0
        for c in corners:
            bm = _apply(glyph[cmap[t][c]]["expected"]["bitmap"], t)
            tot += _match(obs[c][0], bm)
        scores[t] = tot / 4.0
    best = max(scores, key=lambda k: scores[k])
    return {"verdict": "upright" if best == "identity" else best, "scores": scores,
            "glyphs": {c: glyph[c]["expected"]["glyph"] for c in corners},
            "mirrored_set": glyph["tl"]["expected"]["mirrored"],
            "contrast": min(o[1] for o in obs.values())}


def _analyze_order(ras, A, lay) -> dict[str, Any]:
    els = _d.elements_by_id(lay)
    out: dict[str, Any] = {}
    bars = els.get("order_bars")
    if bars and bars.get("drawn", True):
        ex = bars["expected"]
        yc = (bars["rect_mm"][1] + bars["rect_mm"][3]) / 2.0
        dark = []
        for x in ex["slot_centres_x_mm"]:
            dark.append(_lum(_box(ras, A, x, yc, 0.2)) < 110)
        out["order_bars"] = {"filled_found": sum(dark), "filled_expected": ex["filled"], "ok": sum(dark) == ex["filled"] and all(dark[: ex["filled"]])}
    pn = els.get("page_number")
    if pn and pn.get("drawn", True):
        ex = pn["expected"]
        gu = ex["cell_mm"]
        x0, y0 = ex["origin_mm"]
        digits = {d["char"]: d["rows"] for d in ex["digit_bitmaps"]}
        text = ""
        for i in range(len(ex["text"])):
            rect = (x0 + i * 6 * gu, y0, x0 + (i * 6 + 5) * gu, y0 + 7 * gu)
            cells, _ = _grid_cells(ras, A, rect, ["0"] * 7)
            best = max(digits, key=lambda ch: _match(cells, digits[ch]))
            text += best
        out["page_number"] = {"read": text, "expected": ex["text"], "ok": text == ex["text"]}
    sm = els.get("side_marker")
    if sm and sm.get("drawn", True):
        sq = sm["expected"]["square_mm"]
        yc = (sq[1] + sq[3]) / 2.0
        w = sq[2] - sq[0]
        left = _lum(_box(ras, A, sq[0] + 0.25 * w, yc, 0.4))
        right = _lum(_box(ras, A, sq[0] + 0.75 * w, yc, 0.4))
        side = "front" if left < right else "back"
        exp = sm["expected"]["side"]
        out["side_marker"] = {"side_read": side, "expected": exp, "ok": (exp or "front") == side}
    return out


def _analyze_flat(ras, A, el) -> dict[str, Any]:
    x0, y0, x1, y1 = el["rect_mm"]
    # interior profile statistics over a grid of small boxes
    n = 8
    vals = []
    for j in range(n):
        for i in range(n):
            vals.append(_lum(_box(ras, A, x0 + (i + 0.5) * (x1 - x0) / n, y0 + (j + 0.5) * (y1 - y0) / n, 0.5)))
    mean = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
    rows = [sum(vals[j * n : (j + 1) * n]) / n for j in range(n)]
    cols = [sum(vals[j * n + i] for j in range(n)) / n for i in range(n)]
    want = el["expected"]["rgb"][0]
    return {"mean": mean, "expected": want, "error": mean - want, "stdev": sd,
            "row_range": max(rows) - min(rows), "col_range": max(cols) - min(cols)}


def _analyze_cc(ras, A, el) -> dict[str, Any]:
    pats = el["expected"]["patches"]
    s = el.get("samples", [])
    got = [_box(ras, A, p["x_mm"], p["y_mm"], 0.8) for p in s]
    want = [p["rgb"] for p in s]
    des = [_d.delta_e76(g, w) for g, w in zip(got, want)]
    # channel order: which permutation of the measured channels best explains the expected colours
    import itertools

    best, bs = (0, 1, 2), 1e18
    for perm in itertools.permutations(range(3)):
        err = sum(sum(abs(g[perm[i]] - w[i]) for i in range(3)) for g, w in zip(got, want))
        if err < bs:
            best, bs = perm, err
    return {"n": len(got), "mean_delta_e76": sum(des) / len(des), "max_delta_e76": max(des),
            "worst_patch": pats[max(range(len(des)), key=lambda i: des[i])]["name"],
            "channel_order": "".join("rgb"[i] for i in best), "channels_swapped": best != (0, 1, 2)}


def _analyze_ramp(ras, A, el) -> dict[str, Any]:
    ex = el["expected"]
    x0, y0, x1, y1 = el["rect_mm"]
    yc = (y0 + y1) / 2.0
    c0, c1 = ex["from_rgb"], ex["to_rgb"]
    pts = []
    for i in range(21):
        t = (i + 0.5) / 21.0
        got = _box(ras, A, x0 + t * (x1 - x0), yc, 0.25)
        want = _d_lerp(c0, c1, t)
        pts.append((got, want))
    err = [max(abs(g[i] - w[i]) for i in range(3)) for g, w in pts]
    lum = [_lum(g) for g, _ in pts]
    mono = all((lum[i + 1] - lum[i]) * (1 if _lum(c1) > _lum(c0) else -1) > -2.5 for i in range(len(lum) - 1))
    return {"max_abs_err": max(err), "mean_abs_err": sum(err) / len(err), "monotonic": mono}


def _d_lerp(c0, c1, t):
    return [c0[i] + (c1[i] - c0[i]) * t for i in range(3)]


def _tick_centroid(ras, A, x_mm: float, y_mm: float, half_mm: float):
    xs = [x_mm - half_mm + k * 0.03 for k in range(int(2 * half_mm / 0.03) + 1)]
    ws = [255.0 - ras.g(*A(x, y_mm)) for x in xs]
    lo = min(ws)
    ws = [w - lo for w in ws]
    s = sum(ws)
    if s < 1e-6 or max(ws) < 40:
        return None
    xc = sum(x * w for x, w in zip(xs, ws)) / s
    return A(xc, y_mm)[0]


def _tick_centre(ras, A, a_mm: float, cross_mm: float, horizontal: bool, half_mm: float):
    """Sub-pixel centre (px) of a ruler tick: darkness centroid along the ruler axis at a fixed cross position."""
    n = int(2 * half_mm / 0.03) + 1
    pts = []
    for k in range(n):
        a = a_mm - half_mm + k * 0.03
        x, y = (a, cross_mm) if horizontal else (cross_mm, a)
        u, v = A(x, y)
        pts.append((a, 255.0 - ras.g(u, v)))
    lo = min(w for _, w in pts)
    tot = sum(w - lo for _, w in pts)
    if tot < 1e-6 or max(w for _, w in pts) - lo < 40:
        return None
    ac = sum(a * (w - lo) for a, w in pts) / tot
    return A(ac, cross_mm) if horizontal else A(cross_mm, ac)


def _analyze_ruler(ras, A, el, nominal: float, max_ticks: int = 80) -> dict[str, Any]:
    """Scale along one ruler scale: every ``k``-th tick located to sub-pixel, regressed against its position."""
    ex = el["expected"]
    horizontal = ex["axis"] == "x"
    edge = ex["tick_edge_mm"]
    d = 1.0 if ex["ticks_point"] in ("down", "right") else -1.0
    cross = edge + d * 0.7
    a0 = ex["zero_line_mm"]
    pos = ex["ticks"]["pos_mm"]
    pitch = ex["pitch_mm"]
    step = max(1, len(pos) // max_ticks)
    sel = list(range(0, len(pos), step))
    if sel[-1] != len(pos) - 1:
        sel.append(len(pos) - 1)
    ux, uy = A.lin(1.0, 0.0) if horizontal else A.lin(0.0, 1.0)
    nrm = math.hypot(ux, uy)
    ux, uy = ux / nrm, uy / nrm
    xs, ss, rows = [], [], []
    for i in sel:
        c = _tick_centre(ras, A, a0 + pos[i], cross, horizontal, min(0.45 * pitch, 0.45))
        if c is None:
            continue
        xs.append(pos[i])
        ss.append(c[0] * ux + c[1] * uy)
    if len(xs) < 4:
        return {"ticks_found": len(xs), "axis": ex["axis"], "unit": ex["unit"]}
    m, q, rms = _d._linfit(xs, ss)
    baseline = xs[-1] - xs[0]
    end_len_mm = (ss[-1] - ss[0]) / nominal
    return {
        "axis": ex["axis"], "unit": ex["unit"], "ticks_found": len(xs), "ticks_expected": len(pos),
        "px_per_mm": m, "dpi": m * 25.4, "pitch_error_pct": (m / nominal - 1.0) * 100.0, "rms_px": rms,
        "baseline_mm": baseline,
        # absolute error over the long baseline: measured distance of the outermost ticks minus the true one
        "length_error_mm": end_len_mm - baseline,
        "fit_length_error_mm": (m / nominal - 1.0) * baseline,
        "dpi_nominal": nominal * 25.4,
    }


def _analyze_measure(ras, A, el, nominal: float) -> dict[str, Any]:
    ex = el["expected"]
    y = el["rect_mm"][1] - 0.9  # above the bar body: only the two end ticks
    u0 = _tick_centroid(ras, A, ex["x0_mm"], y, 1.0)
    u1 = _tick_centroid(ras, A, ex["x1_mm"], y, 1.0)
    if u0 is None or u1 is None:
        return {"found": False}
    length = (u1 - u0) / nominal
    return {"found": True, "length_mm": length, "expected_mm": ex["length_mm"], "length_error_mm": length - ex["length_mm"],
            "length_error_pct": (length / ex["length_mm"] - 1) * 100.0}


def _analyze_frame(ras, A, el, nominal: float, mono: bool = False, ink_rects=()) -> dict[str, Any]:
    """Frame edge positions.  Colour pages score a probe pixel by ``blue - red`` (the frame is blue).  On a grey /
    mono page that no longer works (blue minus red is zero everywhere), so the score is luminance contrast against the
    local paper level, and a dark run that belongs to other known ink (``ink_rects``) is not taken for the frame."""
    ex = el["expected"]
    out = {}
    probes = {"left": ("x", ex["edges"][0]["pos_mm"]), "right": ("x", ex["edges"][1]["pos_mm"]), "top": ("y", ex["edges"][2]["pos_mm"]), "bottom": ("y", ex["edges"][3]["pos_mm"])}
    x0, y0, x1, y1 = el["rect_mm"]
    frame_lum = _lum(ex["rgb"])
    for name, (axis, pos) in probes.items():
        samples = []
        for k in range(-60, 61):
            d = pos + k * 0.05
            if axis == "x":
                px_, py_ = d, (y0 + y1) / 2.0 + 15.0
            else:
                px_, py_ = (x0 + x1) / 2.0 + 15.0, d
            u, v = A(px_, py_)
            r, g, b = ras.rgb.getpixel((min(max(int(u), 0), ras.w - 1), min(max(int(v), 0), ras.h - 1)))
            samples.append((d, (px_, py_), (r, g, b)))
        found = None
        if not mono:
            best = 0.0
            for d, _, (r, g, b) in samples:
                score = b - r
                if score > best:
                    best, found = score, d
            ok = best > 60
        else:
            paper = max(_lum(c) for _, _, c in samples)
            want = max(25.0, paper - frame_lum)
            thr = 0.4 * want
            runs: list[list[tuple[float, float]]] = []
            cur: list[tuple[float, float]] = []
            for d, pt, c in samples:
                dark = paper - _lum(c)
                explained = any(q[0] - 0.4 <= pt[0] <= q[2] + 0.4 and q[1] - 0.4 <= pt[1] <= q[3] + 0.4 for q in ink_rects)
                if dark > thr and not explained:
                    cur.append((d, dark))
                elif cur:
                    runs.append(cur)
                    cur = []
            if cur:
                runs.append(cur)
            ok = False
            if runs:
                def centre(run):
                    sw = sum(w for _, w in run)
                    return sum(d * w for d, w in run) / sw

                run = min(runs, key=lambda r_: abs(centre(r_) - pos))
                found = centre(run)
                ok = True
        out[name] = {"found": ok, "pos_mm": found if ok else None, "error_mm": (found - pos) if ok else None}
    return {"edges": out, "clipped_edges": [k for k, v in out.items() if not v["found"]], "mode": "mono" if mono else "colour"}


def _analyze_line_pairs(ras, A, el) -> dict[str, Any]:
    ex = el["expected"]
    vertical = ex["orientation"].startswith("vertical")
    x0, y0, x1, y1 = el["rect_mm"]
    p = ex["patch_mm"]
    mods = []
    for i, per in enumerate(ex["periods_mm"]):
        a = x0 + i * p
        vals = []
        n = max(24, int(p / 0.03))
        for k in range(n):
            t = (k + 0.5) / n * p
            s = 0.0
            for j in range(5):
                o = (j + 0.5) / 5 * (y1 - y0 if vertical else x1 - x0)
                if vertical:
                    s += ras.g(*A(a + t, y0 + o))
                else:
                    s += ras.g(*A(x0 + o, y0 + t))
            vals.append(s / 5)
        # first-harmonic amplitude at the expected period (Goertzel-like projection)
        w = 2 * math.pi / per
        re = sum(v * math.cos(w * (k + 0.5) / n * p) for k, v in enumerate(vals)) / n * 2
        im = sum(v * math.sin(w * (k + 0.5) / n * p) for k, v in enumerate(vals)) / n * 2
        mods.append(math.hypot(re, im) / (255.0 * 2 / math.pi))  # 1.0 = ideal square wave fundamental
    ref = mods[0] if mods and mods[0] > 0 else 1.0
    last = None
    for per, m in zip(ex["periods_mm"], mods):
        if m >= 0.3 * ref:
            last = per
    return {"modulation": [round(m, 3) for m in mods], "last_resolvable_period_mm": last}


def _analyze_ladder(ras, A, el) -> dict[str, Any]:
    ex = el["expected"]
    x0, y0, x1, y1 = el["rect_mm"]
    pitch = ex["pitch_mm"]
    neg = ex["polarity"].startswith("reverse")
    res = []
    for i, w_mm in enumerate(ex["widths_mm"]):
        yc = y0 + pitch * (i + 0.5)
        best = None
        for k in range(-6, 7):
            for dx in (0.25, 0.5, 0.75):
                v = ras.g(*A(x0 + dx * (x1 - x0), yc + k * 0.05))
                best = v if best is None else (max(best, v) if neg else min(best, v))
        # contrast of the line against its surround
        side = sum(ras.g(*A(x0 + 0.5 * (x1 - x0), yc + off)) for off in (-pitch * 0.45, pitch * 0.45)) / 2.0
        res.append({"width_pt": ex["widths_pt"][i], "extreme": best, "surround": side, "visible": abs(best - side) > 40})
    vis = [r["width_pt"] for r in res if r["visible"]]
    return {"lines": res, "thinnest_visible_pt": min(vis) if vis else None}


def _analyze_halftone(ras, A, el) -> dict[str, Any]:
    ex = el["expected"]
    x0, y0, x1, y1 = el["rect_mm"]
    cw = (x1 - x0) / 5.0
    hh = (y1 - y0) / 2.0 - 0.3
    out = []
    for i, pct in enumerate(ex["coverage_pct"]):
        xs = x0 + i * cw
        vals = []
        for j in range(6):
            for k in range(4):
                vals.append(_lum(_box(ras, A, xs + (j + 0.5) / 6 * cw, y0 + (k + 0.5) / 4 * hh, 0.3)))
        mean = sum(vals) / len(vals)
        want = 255.0 * (1.0 - pct / 100.0)
        out.append({"coverage_pct": pct, "mean": mean, "expected_mean": want, "error": mean - want})
    return {"cells": out, "max_abs_mean_error": max(abs(c["error"]) for c in out)}


def _analyze_zone(ras, A, el) -> dict[str, Any]:
    x0, y0, x1, y1 = el["rect_mm"]
    half = (x1 - x0) / 2.0
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    out = []
    for frac in (0.2, 0.4, 0.6, 0.8, 0.95):
        r0 = frac * half
        vals = [ras.g(*A(cx + r0 + k * 0.03, cy)) for k in range(int(0.12 * half / 0.03))]
        out.append({"r_frac": frac, "contrast": (max(vals) - min(vals)) / 255.0})
    return {"contrast_by_radius": out, "centre": _lum(_box(ras, A, cx, cy, 0.3))}


def _analyze_text(ras, A, el) -> dict[str, Any]:
    x0, y0, x1, y1 = el["rect_mm"]
    n = 20
    s = 0.0
    for j in range(n):
        for i in range(n):
            s += 255.0 - _lum(_box(ras, A, x0 + (i + 0.5) / n * (x1 - x0), y0 + (j + 0.5) / n * (y1 - y0), 0.1))
    ink = s / (n * n) / 255.0
    ex = el["expected"].get("ink_fraction")
    out = {"ink_fraction_measured": ink}
    if ex is not None:
        out["ink_fraction_expected"] = ex
    return out


def _geometry(A, nominal: float) -> dict[str, Any]:
    dec = A.decompose()
    sx, sy = dec["px_per_mm_x"], dec["px_per_mm_y"]
    return {**dec, "dpi_x": sx * 25.4, "dpi_y": sy * 25.4, "scale_x_vs_nominal": sx / nominal,
            "scale_y_vs_nominal": sy / nominal, "aspect_x_over_y": sx / sy, "origin_px": [A.c, A.f],
            "origin_offset_mm": [A.c / nominal, A.f / nominal], "affine": [A.a, A.b, A.c, A.d, A.e, A.f],
            "mirrored": A.a * A.e - A.b * A.d < 0}


def _fit_from_markers(image, ras, layout, init, nominal, detector):
    """Fallback page fit from the ArUco marker corners (when fewer than three real crosses survive)."""
    seed = _d._Affine(*init) if init else _d._Affine(nominal, 0.0, 0.0, 0.0, nominal, 0.0)
    try:
        det = _m.detect_markers(image, ras, seed, layout, detector=detector)
    except Exception:  # pragma: no cover - detector failure must not hide the crosses' failure report
        return None, None
    mm, px, who = _m.marker_correspondences(det, layout)
    if len(set(who)) < 3 or len(mm) < 8:
        return None, det
    A = _d._Affine.fit(mm, px)
    dec = A.decompose()
    if not (0.4 < dec["px_per_mm_x"] / nominal < 2.5 and 0.4 < dec["px_per_mm_y"] / nominal < 2.5) or abs(dec["skew_deg"]) > 20.0:
        return None, det
    return A, det


def analyze_v3(image, layout: dict[str, Any], scan_dpi: float | None = None, init=None, detector: str = "auto",
               marker_fallback: bool = True) -> dict[str, Any]:
    """Report for a scan of a v3 page.

    ``detector``: ``"auto"`` / ``"python"`` / ``"cv2"`` for the ArUco markers.  The page fit comes from the registration
    crosses (``geometry.fit == "crosses"``); when fewer than three real crosses survive and ``marker_fallback`` is true
    the ArUco marker corners are used instead (``"markers"``); otherwise ``summary.fit`` is ``"failed"`` and nothing
    else is reported (never a fit to clutter).
    """
    ras = _d._Raster(image)
    nominal = (scan_dpi or layout["dpi_effective"]) / 25.4
    A, geo = _fit(ras, layout, _d._Affine(*init) if init else None, scan_dpi)
    mdet = None
    if A is None and marker_fallback:
        crosses_info = dict(geo)
        A, mdet = _fit_from_markers(image, ras, layout, init, nominal, detector)
        if A is not None:
            geo = {**crosses_info, "fit": "markers", "fit_fallback_reason": crosses_info.get("reason")}
    report: dict[str, Any] = {
        "layout_version": 3,
        "layout_revision": layout.get("layout_revision", 0),
        "colour_space": layout["colour_space"],
        "image_px": [ras.w, ras.h],
        "nominal_px_per_mm": nominal,
        "geometry": geo,
        "elements": {},
        "summary": {},
    }
    if A is None:
        report["summary"] = {"fit": "failed", "reason": geo.get("reason"), "crosses_found": geo.get("crosses_found")}
        if any(e.get("kind") == "marker" and e.get("drawn", True) for e in layout["elements"]):
            # no page fit, but a page that arrived turned by 90 degrees or transposed can still be named from its markers
            try:
                o = _m.detect_orientation(image, layout)
            except Exception:  # pragma: no cover - the failure report must survive
                o = None
            if o and o.get("available") and not o.get("ambiguous"):
                report["markers"] = {"orientation": o}
                report["summary"]["marker_orientation"] = o["verdict"]
        return report
    geo.update(_geometry(A, nominal))
    # independent check: the ArUco markers must be where the fit says (needs a revision-1 layout)
    has_markers = any(e.get("kind") == "marker" and e.get("drawn", True) for e in layout["elements"])
    mrep = None
    if has_markers:
        if mdet is None or mdet.get("detector") == "python":
            mdet = _m.detect_markers(image, ras, A, layout, detector=detector)
        mrep = _m.marker_report(mdet, layout, A)
        n_exp, n_found = mrep["n_expected"], mrep["n_found"]
        agree = n_found >= max(2, n_exp // 2) and mrep.get("max_corner_err_mm", 99.0) < 2.0
        geo["marker_check"] = {"detector": mrep["detector"], "markers_found": n_found, "markers_expected": n_exp,
                               "agree": bool(agree), "max_corner_err_mm": mrep.get("max_corner_err_mm")}
        if geo.get("fit") == "crosses" and geo.get("crosses_found", 4) < 4 and not agree and n_exp >= 4:
            report["summary"] = {"fit": "failed", "reason": "three crosses only and the markers do not confirm the fit",
                                 "crosses_found": geo.get("crosses_found")}
            report["markers"] = mrep
            return report
    mono = _is_mono(ras)
    ink_rects = [e["rect_mm"] for e in layout["elements"]
                 if e.get("drawn", True) and e["group"] not in ("print_marks", "dropped") and e["kind"] != "frame"]
    out = report["elements"]
    boards = _m.analyze_boards(image, ras, A, layout, use_cv2=(detector != "python"))
    for el in layout["elements"]:
        if not el.get("drawn", True) or el["group"] == "dropped":
            continue
        kind, eid = el["kind"], el["id"]
        try:
            r: dict[str, Any] = {}
            if eid == "colorchecker24":
                r = _analyze_cc(ras, A, el)
            elif kind == "ramp":
                r = _analyze_ramp(ras, A, el)
            elif kind == "flat_field":
                r = _analyze_flat(ras, A, el)
                r.update({k: v for k, v in _samples(ras, A, el, 0.8).items() if k in ("max_abs_err",)})
            elif kind == "marker":
                r = dict(mrep["per_marker"].get(eid, {})) if mrep else {}
            elif kind == "ruler":
                r = _analyze_ruler(ras, A, el, nominal)
            elif kind == "measure_bar":
                r = _analyze_measure(ras, A, el, nominal)
            elif eid == "printable_frame":
                r = _analyze_frame(ras, A, el, nominal, mono=mono, ink_rects=ink_rects)
            elif kind == "line_pairs":
                r = _analyze_line_pairs(ras, A, el)
            elif kind == "line_ladder":
                r = _analyze_ladder(ras, A, el)
            elif kind == "halftone":
                r = _analyze_halftone(ras, A, el)
            elif kind == "zone_plate":
                r = _analyze_zone(ras, A, el)
            elif kind == "text" and "ink_fraction" in el["expected"]:
                r = _analyze_text(ras, A, el)
            elif eid in boards:
                r = boards[eid]
            elif el.get("samples"):
                r = _samples(ras, A, el)
                r.pop("samples", None)
            else:
                continue
            out[eid] = r
        except Exception as e:  # pragma: no cover - keep one bad element from hiding the rest
            out[eid] = {"error": repr(e)}
    orient = _analyze_orientation(ras, A, layout)
    order = _analyze_order(ras, A, layout)
    out["orientation"] = orient
    out["page_order"] = order
    if mrep:
        report["markers"] = mrep
    # rulers: dpi per axis per unit, absolute error over the long baseline (the longest ruler of each axis / unit)
    rulers: dict[str, dict[str, Any]] = {}
    for eid, v in out.items():
        if isinstance(v, dict) and "px_per_mm" in v and "baseline_mm" in v and "axis" in v:
            cur = rulers.setdefault(v["axis"], {}).get(v["unit"])
            if cur is None or v["baseline_mm"] > cur["baseline_mm"]:
                rulers[v["axis"]][v["unit"]] = {"element": eid, "dpi": v["dpi"], "baseline_mm": v["baseline_mm"],
                                                "length_error_mm": v["length_error_mm"], "pitch_error_pct": v["pitch_error_pct"]}
    report["rulers"] = rulers
    mb = out.get("measure_bar", {})
    summary: dict[str, Any] = {
        "fit": geo.get("fit", "crosses"),
        "scale_x_vs_nominal": geo["scale_x_vs_nominal"], "scale_y_vs_nominal": geo["scale_y_vs_nominal"],
        "aspect_x_over_y": geo["aspect_x_over_y"], "rotation_deg": geo["rotation_deg"], "skew_deg": geo["skew_deg"],
        "mirrored": geo["mirrored"],
        "origin_offset_mm": geo["origin_offset_mm"], "orientation": orient.get("verdict"),
    }
    if mrep:
        summary["markers_found"] = mrep["n_found"]
        summary["markers_expected"] = mrep["n_expected"]
        summary["marker_detector"] = mrep["detector"]
        summary["marker_orientation"] = mrep["orientation"]["verdict"]
        if "rms_corner_err_mm" in mrep:
            summary["marker_rms_corner_err_mm"] = mrep["rms_corner_err_mm"]
        if "homography" in mrep:
            summary["homography_residual_rms_mm"] = mrep["homography"]["residual_rms_mm"]
    if "colorchecker24" in out:
        summary["colorchecker_mean_delta_e76"] = out["colorchecker24"]["mean_delta_e76"]
        summary["channel_order"] = out["colorchecker24"]["channel_order"]
    flats = [v for k, v in out.items() if k.startswith("flat_") and "stdev" in v]
    if flats:
        summary["flat_field_max_stdev"] = max(v["stdev"] for v in flats)
        summary["flat_field_max_row_range"] = max(v["row_range"] for v in flats)
    if "printable_frame" in out:
        summary["clipped_edges"] = out["printable_frame"]["clipped_edges"]
        summary["margin_errors_mm"] = {k: v["error_mm"] for k, v in out["printable_frame"]["edges"].items()}
    for rid in ("ruler_metric", "ruler_inch", "ruler_v_metric", "ruler_v_inch"):
        if rid in out and "pitch_error_pct" in out[rid]:
            summary[rid + "_pitch_error_pct"] = out[rid]["pitch_error_pct"]
    for axis, units in rulers.items():
        for unit, v in units.items():
            summary[f"ruler_{axis}_{unit}_dpi"] = v["dpi"]
            summary[f"ruler_{axis}_{unit}_length_error_mm"] = v["length_error_mm"]
    if mb.get("found"):
        summary["measure_bar_length_error_mm"] = mb["length_error_mm"]
        summary["measure_bar_length_error_pct"] = mb["length_error_pct"]
    for k in ("order_bars", "page_number", "side_marker"):
        if k in order:
            summary[k + "_ok"] = order[k]["ok"]
    if "page_number" in order:
        summary["page_number_read"] = order["page_number"]["read"]
    if "side_marker" in order:
        summary["side_read"] = order["side_marker"]["side_read"]
    report["summary"] = summary
    return report
