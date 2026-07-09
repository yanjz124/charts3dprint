"""
Completeness pass: recover any ink the vector extractor missed.

get_drawings() returns vector art + Type3 glyphs, but NOT regular embedded-font
text (e.g. FAA margin/date marginalia). To guarantee 100% of features, render the
page, find ink pixels not covered by the vectors, and add them back as polygons
in their nearest PDF color. At >=300 DPI this is finer than a 0.2 mm nozzle, so
there's no loss at print scale.
"""
import numpy as np
import fitz
from scipy.ndimage import binary_dilation
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MPath
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .build import _polys


def _rasterize_vectors(feats, W, H, Wp, Hp, dpi):
    allg = unary_union([p for _, p in feats.fills]
                       + [l.buffer(max(w / 2, 0.35)) for _, l, w in feats.strokes])
    fig = plt.figure(figsize=(Wp / dpi, Hp / dpi), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W); ax.set_ylim(H, 0); ax.axis("off")
    for poly in _polys(allg):
        ext = list(poly.exterior.coords)
        v = ext[:]; c = [MPath.MOVETO] + [MPath.LINETO] * (len(ext) - 2) + [MPath.CLOSEPOLY]
        for r in poly.interiors:
            rr = list(r.coords); v += rr
            c += [MPath.MOVETO] + [MPath.LINETO] * (len(rr) - 2) + [MPath.CLOSEPOLY]
        ax.add_patch(PathPatch(MPath(v, c), facecolor="k", edgecolor="none"))
    fig.canvas.draw()
    a = np.frombuffer(fig.canvas.buffer_rgba(), np.uint8).reshape(
        fig.canvas.get_width_height()[::-1] + (4,))[:, :, 0]
    plt.close(fig)
    return a[:Hp, :Wp] < 128


def _polygonize(mask, s):
    """Binary mask -> shapely geometry (even-odd holes), scaled px -> PDF points."""
    fig = plt.figure()
    ax = fig.add_subplot(111)
    cs = ax.contour(mask.astype(float), levels=[0.5])
    segs = list(cs.allsegs[0]) if cs.allsegs else []
    plt.close(fig)
    polys = []
    for seg in segs:
        if len(seg) >= 4:
            try:
                p = Polygon(seg / s)
                if not p.is_valid:
                    p = p.buffer(0)
                if not p.is_empty and p.area > 0:
                    polys.append(p)
            except Exception:
                pass
    if not polys:
        return None
    g = polys[0]
    for p in polys[1:]:
        try:
            g = g.symmetric_difference(p)
        except Exception:
            g = unary_union([g, p])
    if not g.is_valid:            # keep raster-derived contours clean for meshing
        g = g.buffer(0)
    return g if not g.is_empty else None


def add_completeness(pdf_path, feats, dpi=350, dilate=2, min_gap_px=20):
    """Find ink missed by the vectors and append it to feats (in-place)."""
    page = fitz.open(pdf_path)[0]
    W, H = page.rect.width, page.rect.height
    s = dpi / 72.0
    pm = page.get_pixmap(matrix=fitz.Matrix(s, s))
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(
        pm.height, pm.width, pm.n)[:, :, :3].astype(np.int16)
    Hp, Wp = rgb.shape[:2]
    nonwhite = rgb.min(axis=2) < 230

    vmask = binary_dilation(_rasterize_vectors(feats, W, H, Wp, Hp, dpi), iterations=dilate)
    gap = nonwhite & ~vmask
    if gap.sum() < min_gap_px:
        return 0

    pal = sorted(feats.colors) or ["#000000"]
    palrgb = np.array([[int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)] for c in pal])
    pal_chroma = (palrgb.max(1) - palrgb.min(1)) > 40   # chromatic palette entries
    gy, gx = np.where(gap)
    px = rgb[gy, gx]
    d = ((px[:, None, :] - palrgb[None, :, :]) ** 2).sum(2).astype(float)
    # A near-gray pixel (e.g. an anti-aliased edge of black text) must not snap to
    # a chromatic filament just because a dark color is numerically closer in RGB.
    low_sat = (px.max(1) - px.min(1)) < 40
    d[low_sat[:, None] & pal_chroma[None, :]] = 1e18
    idx = d.argmin(1)

    added = 0
    for i, c in enumerate(pal):
        sel = idx == i
        if not sel.any():
            continue
        sub = np.zeros((Hp, Wp), bool)
        sub[gy[sel], gx[sel]] = True
        g = _polygonize(sub, s)
        if g is not None and not g.is_empty:
            feats.completeness_fills.append((c, g))
            feats.colors.add(c)
            added += 1
    return added
