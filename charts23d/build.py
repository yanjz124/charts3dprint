"""
Turn extracted Features into printable geometry: scale/fit to the bed, union
per PDF color, extrude to a relief on a base plate, export STL + 3MF.

Objects are keyed by the PDF color hex ('#rrggbb'); "base" is the white plate.
"""
from dataclasses import dataclass, field
import numpy as np
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import unary_union
from shapely.affinity import affine_transform
import trimesh
try:
    import manifold3d as _m3d
except Exception:
    _m3d = None

BASE = "base"
BASE_COLOR = "#FFFFFF"


@dataclass
class Options:
    width_mm: float = 200.0      # target chart content width (ignored if fit_bed)
    fit_bed: bool = False
    bed_mm: float = 256.0        # Bambu P2S build plate
    margin_mm: float = 3.0       # empty border kept inside the bed
    nozzle_mm: float = 0.2       # drives minimum feature width
    min_line_mm: float = None    # guaranteed min feature width (None -> nozzle);
                                 # thin strokes AND thin fills grow to this so no
                                 # feature is dropped as too-thin at print time
    relief_mm: float = 1.2       # relief height (flat mode)
    base_mm: float = 2.0
    engrave: bool = False        # recess features into the plate instead of raising
    # Layered (color-by-height) mode: minimal filament swaps.
    layered: bool = False
    min_swaps: bool = False      # non-overlapping slabs -> #colors swaps total
    band_mm: float = 0.4         # thickness of each color band
    layer_h: float = 0.12        # print layer height (for color-change layer #s)
    order: tuple = None          # explicit bottom->top color order; None = auto

    def min_line(self):
        # default to 2 nozzle widths (2 perimeters) so every line prints reliably
        return self.min_line_mm if self.min_line_mm else 2.0 * self.nozzle_mm


@dataclass
class Plan:
    scale: float                 # mm per PDF point
    size_mm: tuple               # (w, h) of the base plate
    geoms: dict                  # color_hex -> shapely (Multi)Polygon in mm, y-up
    order: list                  # list of GROUPS (each a list of hexes), bottom->top
    flat: list                   # color_hex list bottom->top (groups flattened)
    colors: dict                 # object name -> display hex (incl. "base")
    base_rect: Polygon
    opts: Options
    stats: dict = field(default_factory=dict)


def _content_bbox(feats):
    parts = ([p for _, p in feats.fills] + [p for _, p in feats.completeness_fills]
             + [ls for _, ls, _ in feats.strokes])
    u = unary_union(parts)
    return u.bounds  # (minx, miny, maxx, maxy) in PDF points, y-down


def _polys(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    return [g for g in geom.geoms if isinstance(g, Polygon) and not g.is_empty]


def _simplify(geom, tol):
    try:
        s = geom.simplify(tol, preserve_topology=True)
        if s.is_valid and not s.is_empty:
            return s
    except Exception:
        pass
    return geom


def _clean_holes(geom, min_hole_mm):
    """
    Keep interior holes (letter counters, boxes) that are big enough to print;
    fill only holes too tiny to reproduce (a gap narrower than the nozzle can't
    be printed anyway). A hole survives if it doesn't vanish when eroded by half
    the min printable gap.
    """
    r = min_hole_mm / 2.0
    polys = []
    for poly in _polys(geom):
        keep = [ring for ring in poly.interiors
                if not Polygon(ring).buffer(-r).is_empty]
        polys.append(Polygon(poly.exterior, keep))
    if not polys:
        return geom
    return unary_union(polys)


def _enforce_min_width(geom, min_mm):
    """
    Grow any feature thinner than `min_mm` up to that width so it survives at
    print time, while leaving already-thick regions essentially unchanged.
    Thin bits = geom minus its morphological opening; grow only those.
    """
    if geom.is_empty:
        return geom
    r = min_mm / 2.0
    try:
        opened = geom.buffer(-r).buffer(r)
        thin = geom.difference(opened)
        if thin.is_empty:
            return geom
        return unary_union([geom, thin.buffer(r, cap_style=1, join_style=1)])
    except Exception:
        return geom


def plan(feats, opts):
    minx, miny, maxx, maxy = _content_bbox(feats)
    cw_pt, ch_pt = (maxx - minx), (maxy - miny)

    if opts.fit_bed:
        usable = opts.bed_mm - 2 * opts.margin_mm
        scale = min(usable / cw_pt, usable / ch_pt)
    else:
        scale = opts.width_mm / cw_pt

    min_line = opts.min_line()
    half_min_pt = (min_line / scale) / 2.0
    m = opts.margin_mm
    A = [scale, 0, 0, -scale, m - minx * scale, m + maxy * scale]

    # Strokes get a uniform min-width buffer up front (consistent line weight).
    # Fills get the thin-part grow separately, so strokes are never re-processed
    # and their weight stays even along the whole line.
    fill_b, stroke_b = {}, {}
    for c, poly in feats.fills:
        fill_b.setdefault(c, []).append(poly)
    for c, ls, w in feats.strokes:
        stroke_b.setdefault(c, []).append(
            ls.buffer(max(w / 2.0, half_min_pt), cap_style=2, join_style=2))

    # Recovered ink (embedded-font text) is kept separate so it lands ON TOP of
    # any white carve-outs (e.g. the date's own white background box).
    comp_b = {}
    for c, poly in feats.completeness_fills:
        comp_b.setdefault(c, []).append(poly)

    # Explicit white fills are intentional marks drawn over other colors (white
    # dots, mask boxes) -> carve them out so the white base shows through.
    white_mm = None
    if feats.white_fills:
        wm = affine_transform(unary_union(feats.white_fills), A)
        if not wm.is_empty:
            white_mm = wm

    cols = set(fill_b) | set(stroke_b) | set(comp_b)
    groups = _normalize_order(opts.order, cols)
    if groups is None:
        groups = [[c] for c in feats.order() if c in cols]
    else:  # append any present colors the user didn't mention, on top
        mentioned = {c for g in groups for c in g}
        groups += [[c] for c in feats.order() if c in cols and c not in mentioned]
    flat = [c for g in groups for c in g]

    nozzle = opts.nozzle_mm
    geoms = {}
    for c in flat:
        parts = []
        if c in fill_b:
            fu = affine_transform(unary_union(fill_b[c]), A)
            # grow fills only up to the nozzle (not the full line weight) so text
            # stems reach printability without crushing their counters
            parts.append(_enforce_min_width(fu, nozzle))
        if c in stroke_b:
            parts.append(affine_transform(unary_union(stroke_b[c]), A))  # already uniform
        u = unary_union(parts) if parts else box(0, 0, 0, 0)
        u = _clean_holes(u, nozzle)   # counters: fill only holes too tiny to print
        if white_mm is not None and not u.is_empty:
            u = u.difference(white_mm)   # carve intentional white dots/marks (kept)
        if c in comp_b:                  # recovered text/ink sits on top of carve-outs
            cu = _enforce_min_width(affine_transform(unary_union(comp_b[c]), A), nozzle)
            u = unary_union([u, cu])
        u = _simplify(u, nozzle * 0.4)   # collapse raster stair-steps -> far fewer faces
        if not u.is_empty:
            geoms[c] = u
    groups = [[c for c in g if c in geoms] for g in groups]
    groups = [g for g in groups if g]
    flat = [c for g in groups for c in g]

    pw = cw_pt * scale + 2 * m
    ph = ch_pt * scale + 2 * m
    colors = {c: c for c in flat}
    colors[BASE] = BASE_COLOR

    p = Plan(scale=scale, size_mm=(pw, ph), geoms=geoms, order=groups, flat=flat,
             colors=colors, base_rect=box(0, 0, pw, ph), opts=opts)
    p.stats = _stats(p)
    return p


def _normalize_order(order, present):
    """order: None, or an iterable whose items are each a color hex (own level) or
    an iterable of hexes (same level). Returns list of groups (lists of present
    hexes), or None."""
    if not order:
        return None
    groups = []
    for item in order:
        g = ([c for c in item if c in present]
             if isinstance(item, (list, tuple, set)) else
             ([item] if item in present else []))
        if g:
            groups.append(list(g))
    return groups or None


def _stats(p):
    """Fine-detail survival % at the chosen nozzle (union of all colored features
    -- dominated by the thin dark linework/text)."""
    if not p.geoms:
        return {}
    u = unary_union(list(p.geoms.values()))
    A0 = u.area
    if A0 <= 0:
        return {}
    r = p.opts.nozzle_mm / 2.0
    opened = u.buffer(-r).buffer(r)
    return {"survive_pct": 100.0 * opened.area / A0, "nozzle_mm": p.opts.nozzle_mm}


def _base_mesh(p):
    pw, ph = p.size_mm
    base = trimesh.creation.box(extents=[pw, ph, p.opts.base_mm])
    base.apply_translation([pw / 2, ph / 2, p.opts.base_mm / 2])
    return base


def _extrude_manifold(geom, height, z0):
    """Extrude a (multi)polygon to a single guaranteed-manifold solid via
    manifold3d: touching features merge instead of leaving non-manifold shared
    edges. Returns None if manifold3d is unavailable or fails."""
    if _m3d is None:
        return None
    if not geom.is_valid:                 # clean invalid contours (e.g. from raster)
        geom = geom.buffer(0)
        if geom.is_empty:
            return None
    contours = []
    for poly in _polys(geom):
        if poly.area <= 0:
            continue
        poly = orient(poly, 1.0)          # exterior CCW, holes CW
        contours.append(np.asarray(poly.exterior.coords[:-1]))
        contours.extend(np.asarray(r.coords[:-1]) for r in poly.interiors)
    if not contours:
        return None
    try:
        cs = _m3d.CrossSection(contours, fillrule=_m3d.FillRule.Positive)
        man = cs.extrude(height)
        try:                              # drop sub-tolerance vertices (stay manifold)
            man = man.simplify(0.01)      # so export rounding / slicer weld can't
        except Exception:                 # collapse near-coincident verts -> non-manifold
            pass
        if z0:
            man = man.translate([0.0, 0.0, z0])
        mesh = man.to_mesh()
        F = np.asarray(mesh.tri_verts)
        if len(F) == 0:
            return None
        V = np.asarray(mesh.vert_properties)[:, :3]
        return trimesh.Trimesh(vertices=V, faces=F, process=False)
    except Exception:
        return None


def _extrude_concat(geom, height, z0):
    parts = []
    for poly in _polys(geom):
        if poly.area <= 0:
            continue
        try:
            parts.append(trimesh.creation.extrude_polygon(poly, height=height))
        except Exception:
            continue
    if not parts:
        return None
    mesh = trimesh.util.concatenate(parts)
    mesh.apply_translation([0, 0, z0])
    return mesh


def _extrude_union(geom, height, z0):
    m = _extrude_manifold(geom, height, z0)
    return m if m is not None else _extrude_concat(geom, height, z0)


def color_change_plan(p):
    """Layered mode: Z heights + slicer layer numbers of each band (level) change,
    bottom->top. Colors sharing a level are joined with '+'."""
    opts = p.opts
    changes, z = [], opts.base_mm
    prev = "white(base)"
    for group in p.order:
        label = "+".join(group)
        changes.append({"at_z_mm": round(z, 3),
                        "layer": int(round(z / opts.layer_h)) + 1,
                        "from": prev, "to": label})
        prev = label
        z += opts.band_mm
    return changes, z


def build_meshes_layered(p):
    """
    Solid color-by-height columns: each color is extruded as a SOLID block of its
    own color from the base up to its level's top height, so e.g. black marks are
    black all the way through (not a thin cap over a gray riser). A color's
    footprint excludes anything stacked above it, so columns never overlap and the
    slicer sees unambiguous per-object colors.
    """
    opts = p.opts
    meshes = {BASE: _base_mesh(p)}
    for gi, group in enumerate(p.order):
        top = opts.base_mm + (gi + 1) * opts.band_mm
        above = [p.geoms[c] for gg in p.order[gi + 1:] for c in gg]
        above_u = unary_union(above) if above else None
        for c in group:
            g = p.geoms[c] if above_u is None else p.geoms[c].difference(above_u)
            if g.is_empty:
                continue
            mm = _extrude_union(g, height=top - opts.base_mm, z0=opts.base_mm)
            if mm is not None:
                meshes[c] = mm
    return meshes


def build_slabs(p):
    """
    Minimal-swap geometry: each level is a Z band; a color's slab footprint is its
    own area plus every color strictly above it (so higher levels are supported).
    With one color per level this is fully non-overlapping (one filament per print
    layer); colors sharing a level coexist in that band (an extra swap there).

    level gi (colors group[gi]) occupies [base+gi*band, base+(gi+1)*band].
    """
    opts = p.opts
    meshes = {BASE: _base_mesh(p)}
    for gi, group in enumerate(p.order):
        z0 = opts.base_mm + gi * opts.band_mm
        above = [p.geoms[c] for gg in p.order[gi + 1:] for c in gg]
        above_u = unary_union(above) if above else None
        for c in group:
            foot = p.geoms[c] if above_u is None else unary_union([p.geoms[c], above_u])
            mm = _extrude_union(foot, height=opts.band_mm, z0=z0)
            if mm is not None:
                meshes[c] = mm
    return meshes


def remap_plan(bp, mapping=None, order=None):
    """
    Cheaply derive a new Plan from a base Plan by re-labelling/merging colors.
    `mapping`: {pdf_hex -> target_hex or None(drop)}. Colors mapped to the same
    target merge. `order`: list of groups (target hexes) bottom->top; unlisted
    colors auto-appended. Reuses base geometry, so it's fast enough for live UI.
    """
    from .extract import _stack_key
    mapping = mapping or {}
    merged = {}
    for pdf_hex, g in bp.geoms.items():
        t = mapping.get(pdf_hex, pdf_hex)
        if not t:
            continue
        merged.setdefault(t, []).append(g)
    geoms = {}
    for t, gs in merged.items():
        u = unary_union(gs) if len(gs) > 1 else gs[0]
        if not u.is_empty:
            geoms[t] = u

    auto = sorted(geoms, key=_stack_key)
    if order:
        seen, groups = set(), []
        for grp in order:
            g = [c for c in grp if c in geoms and c not in seen]
            seen.update(g)
            if g:
                groups.append(g)
        groups += [[c] for c in auto if c not in seen]
    else:
        groups = [[c] for c in auto]
    flat = [c for g in groups for c in g]

    colors = {c: c for c in flat}
    colors[BASE] = BASE_COLOR
    p = Plan(scale=bp.scale, size_mm=bp.size_mm, geoms=geoms, order=groups, flat=flat,
             colors=colors, base_rect=bp.base_rect, opts=bp.opts)
    p.stats = _stats(p)
    return p


def build_meshes(p):
    opts = p.opts
    if opts.min_swaps:
        return build_slabs(p)
    if opts.layered:
        return build_meshes_layered(p)

    if opts.engrave:
        cutter = _extrude_union(unary_union(list(p.geoms.values())),
                                opts.relief_mm + 0.2, opts.base_mm - opts.relief_mm)
        base = _base_mesh(p)
        if cutter is not None:
            base = base.difference(cutter)   # manifold3d backend
        return {BASE: base}

    meshes = {BASE: _base_mesh(p)}
    for c in p.flat:
        mm = _extrude_union(p.geoms[c], opts.relief_mm, opts.base_mm)
        if mm is not None:
            meshes[c] = mm
    return meshes


def _safe(name):
    return name.lstrip("#") if name.startswith("#") else name


def export(meshes, out_prefix):
    scene = trimesh.Scene()
    written = []
    for name, m in meshes.items():
        stl = f"{out_prefix}_{_safe(name)}.stl"
        m.export(stl)
        written.append(stl)
        scene.add_geometry(m, geom_name=name)
    three = f"{out_prefix}.3mf"
    scene.export(three)
    written.append(three)
    return three, written
