"""
Extract vector features from a PDF into shapely geometry, grouped by the PDF's
own colors (one group per distinct color -> one filament). No semantic buckets:
whatever colors are in the PDF are what you get. Geometry is in PDF-point space.
"""
import collections
import fitz
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

BEZIER_STEPS = 8
_QUANT = 2  # round color channels to this many decimals when grouping


def _hex(rgb):
    return "#{:02X}{:02X}{:02X}".format(
        *(max(0, min(255, round(c * 255))) for c in rgb))


def color_key(rgb):
    """Quantized '#rrggbb' key for a PDF color, or None to skip (white/paper)."""
    if rgb is None:
        return None
    r, g, b = (round(c, _QUANT) for c in rgb)
    if r > 0.93 and g > 0.93 and b > 0.93:
        return None  # white == paper/background, comes from the base plate
    return _hex((r, g, b))


def _rgb(hexcolor):
    h = hexcolor.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


def luminance(hexcolor):
    r, g, b = _rgb(hexcolor)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _stack_key(hexcolor):
    """Bottom -> top ordering key. Neutral (gray/black) colors go first, ordered
    light -> dark (so white base, then gray, then black); chromatic colors (e.g.
    an orange ramp) stack on top."""
    r, g, b = _rgb(hexcolor)
    chromatic = (max(r, g, b) - min(r, g, b)) > 0.15
    return (1 if chromatic else 0, -luminance(hexcolor))


def _pt(p):
    return (p.x, p.y)


def _bezier(p0, p1, p2, p3, n=BEZIER_STEPS):
    out = []
    for i in range(1, n + 1):
        t = i / n
        mt = 1 - t
        out.append((
            mt**3*p0[0] + 3*mt*mt*t*p1[0] + 3*mt*t*t*p2[0] + t**3*p3[0],
            mt**3*p0[1] + 3*mt*mt*t*p1[1] + 3*mt*t*t*p2[1] + t**3*p3[1],
        ))
    return out


def _evenodd_fill(subs):
    """
    Combine a fill's subpaths with the even-odd rule so nested contours become
    holes (letter counters in 0/P/B/A/D/R, etc.) instead of filled solids.
    Even-odd == symmetric difference (XOR) of the subpath polygons.
    """
    polys = []
    for s in subs:
        if len(s) < 3:
            continue
        try:
            p = Polygon(s)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0:
                polys.append(p)
        except Exception:
            pass
    if not polys:
        return None
    try:
        g = polys[0]
        for p in polys[1:]:
            g = g.symmetric_difference(p)
        if not g.is_empty:
            return g
    except Exception:
        pass
    return unary_union(polys)   # fallback: no holes, but at least valid


def _subpaths(items):
    subs, cur = [], []
    for it in items:
        k = it[0]
        if k == "l":
            a, b = _pt(it[1]), _pt(it[2])
            if not cur:
                cur = [a, b]
            elif cur[-1] == a:
                cur.append(b)
            else:
                subs.append(cur); cur = [a, b]
        elif k == "c":
            a, c1, c2, d = _pt(it[1]), _pt(it[2]), _pt(it[3]), _pt(it[4])
            if not cur:
                cur = [a]
            elif cur[-1] != a:
                subs.append(cur); cur = [a]
            cur.extend(_bezier(a, c1, c2, d))
        elif k == "re":
            r = it[1]
            if cur:
                subs.append(cur); cur = []
            subs.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)])
        elif k == "qu":
            q = it[1]
            if cur:
                subs.append(cur); cur = []
            subs.append([_pt(q.ul), _pt(q.ur), _pt(q.lr), _pt(q.ll), _pt(q.ul)])
    if cur:
        subs.append(cur)
    return [s for s in subs if len(s) >= 2]


class Features:
    """Extracted features in PDF-point space (y-down), keyed by PDF color hex."""
    def __init__(self, page_w, page_h):
        self.page_w = page_w
        self.page_h = page_h
        self.fills = []              # (color_hex, Polygon)  vector fills
        self.completeness_fills = [] # (color_hex, Polygon)  recovered from raster, sits on top
        self.white_fills = []        # Polygon  explicit white marks -> carve-outs
        self.strokes = []            # (color_hex, LineString, width_pt)
        self.colors = set()          # distinct color hexes present (excludes white)

    def order(self):
        """Bottom -> top: neutral ramp light->dark (white base, gray, black),
        then chromatic colors (orange ramp, etc.) on top."""
        return sorted(self.colors, key=_stack_key)


def quantize_to_palette(feats, palette_hex):
    """
    Snap every PDF color to the nearest color in `palette_hex` (a list of filament
    '#rrggbb'), merging near-identical tones. Only the given palette are targets --
    existing ink never snaps to the white background unless you list white
    explicitly (then colors nearest white drop to the base).
    """
    targets = [h.upper() for h in palette_hex]
    if not targets:
        return {}
    trgb = {t: _rgb(t) for t in targets}

    def near(c):
        cr = _rgb(c)
        return min(targets, key=lambda t: sum((trgb[t][i] - cr[i]) ** 2 for i in range(3)))

    def _is_white(h):
        return all(v > 0.93 for v in _rgb(h))

    remap = {c: near(c) for c in feats.colors}
    keep = lambda c: not _is_white(remap[c])
    feats.fills = [(remap[c], g) for c, g in feats.fills if keep(c)]
    feats.completeness_fills = [(remap[c], g) for c, g in feats.completeness_fills if keep(c)]
    feats.strokes = [(remap[c], ls, w) for c, ls, w in feats.strokes if keep(c)]
    feats.colors = {v for v in remap.values() if v != "#FFFFFF"}
    return remap


def extract(pdf_path):
    doc = fitz.open(pdf_path)
    page = doc[0]
    feats = Features(page.rect.width, page.rect.height)
    for d in page.get_drawings():
        subs = _subpaths(d["items"])
        if not subs:
            continue
        fill_c = d.get("fill")
        fk = color_key(fill_c)
        sk = color_key(d.get("color"))
        w = d.get("width") or 0.0
        if fk:
            g = _evenodd_fill(subs)
            if g is not None and not g.is_empty:
                feats.fills.append((fk, g))
                feats.colors.add(fk)
        elif fill_c is not None and min(fill_c) > 0.93:   # explicit white mark
            g = _evenodd_fill(subs)
            if g is not None and not g.is_empty:
                feats.white_fills.append(g)
        if sk:
            for s in subs:
                try:
                    feats.strokes.append((sk, LineString(s), w))
                    feats.colors.add(sk)
                except Exception:
                    pass
    return feats
