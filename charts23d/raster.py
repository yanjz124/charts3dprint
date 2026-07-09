"""
Raster-chart support. Some charts (e.g. many Jeppesen PDFs) are a single
flattened bitmap with no vector paths. This renders the page, quantizes it to a
handful of colors, and turns each color into shapely polygons — so raster charts
flow through the same build/preview/color-editing pipeline as vector ones.
"""
import numpy as np
import fitz
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from shapely.geometry import Polygon
from shapely.strtree import STRtree
from shapely.ops import unary_union

from .extract import Features, _hex


def is_raster(pdf_path):
    """True if the page is essentially an image with (almost) no vector paths."""
    page = fitz.open(pdf_path)[0]
    vector_items = sum(len(d["items"]) for d in page.get_drawings())
    return len(page.get_images()) >= 1 and vector_items < 50


def _polygonize_mask(mask, s):
    """
    Binary mask -> shapely geometry (with holes), scaled from pixels (s px per pt)
    to PDF points. Even-odd holes resolved by containment depth via an STRtree,
    so thousands of separate marks stay fast (no pairwise ops).
    """
    m = np.pad(mask, 1, constant_values=False)
    fig = plt.figure()
    ax = fig.add_subplot(111)
    cs = ax.contour(m.astype(float), levels=[0.5])
    segs = [seg for seg in (cs.allsegs[0] if cs.allsegs else []) if len(seg) >= 4]
    plt.close(fig)

    polys = []
    for seg in segs:
        try:
            p = Polygon((seg - 1.0) / s)          # undo pad, px -> pt
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0:
                polys.append(p)
        except Exception:
            pass
    if not polys:
        return None

    reps = [p.representative_point() for p in polys]
    tree = STRtree(polys)
    depth = [0] * len(polys)
    for i, rp in enumerate(reps):
        for j in tree.query(rp):
            if j != i and polys[j].contains(rp):
                depth[i] += 1

    out = []
    for i, p in enumerate(polys):
        if depth[i] % 2:                          # a hole -> handled by its parent
            continue
        holes = [polys[j].exterior.coords for j in range(len(polys))
                 if depth[j] == depth[i] + 1 and p.contains(reps[j])]
        try:
            out.append(Polygon(p.exterior.coords, holes))
        except Exception:
            out.append(p)
    u = unary_union(out)
    return u if not u.is_empty else None


def extract_raster(pdf_path, dpi=150, palette_n=16, sat_chroma=12,
                   ink_lum=150, chroma_round=40, min_px=40):
    """
    Render + separate a raster page by color. Low-saturation pixels split by
    lightness: dark -> one 'ink' (black), light -> background (dropped) so gray
    anti-aliasing and paper don't flood the model. Chromatic pixels (blue water,
    brown terrain, ...) are kept as their own colors (near tones merged).
    """
    page = fitz.open(pdf_path)[0]
    W, H = page.rect.width, page.rect.height
    s = dpi / 72.0
    pm = page.get_pixmap(matrix=fitz.Matrix(s, s))
    rgb = np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.width, pm.n)[:, :, :3]

    idx_img = Image.fromarray(rgb).quantize(
        colors=palette_n, method=Image.MEDIANCUT, dither=Image.NONE)
    arr = np.asarray(idx_img)
    pal = idx_img.getpalette()[:palette_n * 3]

    # map each palette index -> a bucket color hex (or skip as background)
    buckets = {}                                   # bucket_hex -> [palette indices]
    for k in range(palette_n):
        r, g, b = pal[3 * k:3 * k + 3]
        sat = max(r, g, b) - min(r, g, b)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        if sat < sat_chroma:                       # neutral (gray/black/paper)
            if lum >= ink_lum:
                continue                           # light -> background
            key = "#000000"                        # dark -> one ink color
        else:                                      # chromatic: merge near tones
            rr = [min(255, round(v / chroma_round) * chroma_round) for v in (r, g, b)]
            key = _hex((rr[0] / 255, rr[1] / 255, rr[2] / 255))
        buckets.setdefault(key, []).append(k)

    feats = Features(W, H)
    for hexc, idxs in sorted(buckets.items(),
                             key=lambda kv: -np.isin(arr, kv[1]).sum()):
        mask = np.isin(arr, idxs)
        if mask.sum() < min_px:
            continue
        g = _polygonize_mask(mask, s)
        if g is None or g.is_empty:
            continue
        g = g.simplify(0.3, preserve_topology=True)
        if not g.is_empty:
            feats.fills.append((hexc, g))
            feats.colors.add(hexc)
    return feats
