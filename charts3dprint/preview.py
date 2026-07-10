"""Render a color-separated PNG preview of a Plan (mm space, y-up), using the
PDF's own colors."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch
from matplotlib.path import Path

from .build import _polys, BASE_COLOR


def _patch(poly, facecolor):
    ext = list(poly.exterior.coords)
    verts = ext[:]
    codes = [Path.MOVETO] + [Path.LINETO] * (len(ext) - 2) + [Path.CLOSEPOLY]
    for ring in poly.interiors:
        r = list(ring.coords)
        verts += r
        codes += [Path.MOVETO] + [Path.LINETO] * (len(r) - 2) + [Path.CLOSEPOLY]
    return PathPatch(Path(verts, codes), facecolor=facecolor, edgecolor="none")


def _figure(p, title=None, dpi=150):
    pw, ph = p.size_mm
    fig, ax = plt.subplots(figsize=(pw / 25.4, ph / 25.4))
    ax.add_patch(_patch(p.base_rect, BASE_COLOR))
    for c in p.flat:               # bottom -> top: later colors drawn on top
        for poly in _polys(p.geoms[c]):
            ax.add_patch(_patch(poly, c))
    ax.set_xlim(0, pw)
    ax.set_ylim(0, ph)
    ax.set_aspect("equal")
    ax.axis("off")
    if title:
        ax.set_title(title, fontsize=8)
    return fig


def render(p, out_png, title=None):
    fig = _figure(p, title)
    fig.savefig(out_png, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


def render_bytes(p, title=None, dpi=110):
    import io
    fig = _figure(p, title, dpi)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()
