"""
Write a Bambu/Orca-compatible multicolor 3MF: each object is bound to a colored
base material and tagged with a 1-based filament (extruder) slot, so the model
opens already colored — no manual assignment. Self-contained (stdlib + trimesh).

Format adapted from the proven map2stl exporter.
"""
import os
import zipfile

import numpy as np
from trimesh.grouping import group_rows

# Filament colors per bucket (white base so paper reads white).
FILAMENT_COLOR = {
    "base": "#FFFFFF",
    "apron": "#B3B3B3",
    "building": "#8F5320",
    "ink": "#111111",
}

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="config" ContentType="text/xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""

_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def hex_to_rgba(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]


def _display_color(hex_color):
    r, g, b, a = hex_to_rgba(hex_color)
    return f"#{r:02X}{g:02X}{b:02X}{a:02X}"


def assign_extruders(names, colors):
    """1-based filament slot per object; equal colors share a slot."""
    color_to_ext, ext, nxt = {}, {}, 1
    for name in names:
        c = colors[name].lower()
        if c not in color_to_ext:
            color_to_ext[c] = nxt
            nxt += 1
        ext[name] = color_to_ext[c]
    return ext


def write_colored_3mf(meshes, colors, out_path, order=None, center=True):
    """
    meshes: {name: trimesh.Trimesh in mm}, colors: {name: '#rrggbb'}.
    Returns a summary dict. Writes a pre-colored Bambu 3MF.
    """
    names = [n for n in (order or list(meshes)) if n in meshes]
    items = [(n, meshes[n].copy()) for n in names
             if meshes[n] is not None and len(meshes[n].faces)]
    if not items:
        raise RuntimeError("No geometry to export.")

    allv = np.vstack([m.vertices for _, m in items])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    if center:
        offset = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
        for _, m in items:
            m.apply_translation(-offset)

    ext = assign_extruders([n for n, _ in items], colors)
    _write(out_path, items, colors, ext)

    return {
        "objects": len(items),
        "size_mm": (hi - lo).round(2).tolist(),
        "filaments": ext,
        "n_filaments": max(ext.values()),
        "colors": {n: colors[n] for n, _ in items},
    }


def _write(out_path, items, colors, ext):
    bases, objects, build, settings = [], [], [], []
    for name, _ in items:
        bases.append(f'   <base name="{name}" displaycolor="{_display_color(colors[name])}"/>')

    for i, (name, m) in enumerate(items):
        oid = i + 1
        vbuf = "".join(f'<vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>'
                       for v in m.vertices)
        tbuf = "".join(f'<triangle v1="{int(f[0])}" v2="{int(f[1])}" v3="{int(f[2])}"/>'
                       for f in m.faces)
        objects.append(
            f'  <object id="{oid}" type="model" pid="100" pindex="{i}" name="{name}">\n'
            f'   <mesh><vertices>{vbuf}</vertices><triangles>{tbuf}</triangles></mesh>\n'
            f'  </object>')
        build.append(f'  <item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 0 0 0" printable="1"/>')
        settings.append(
            f'  <object id="{oid}">\n'
            f'    <metadata key="name" value="{name}"/>\n'
            f'    <metadata key="extruder" value="{ext[name]}"/>\n'
            f'  </object>')

    model = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<model unit="millimeter" xml:lang="en-US" xmlns="{_NS}">',
        ' <metadata name="Application">charts3dprint</metadata>',
        ' <resources>',
        '  <basematerials id="100">',
        "\n".join(bases),
        '  </basematerials>',
        "\n".join(objects),
        ' </resources>',
        ' <build>',
        "\n".join(build),
        ' </build>',
        '</model>',
        '',
    ])
    model_settings = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<config>',
        "\n".join(settings),
        '</config>',
        '',
    ])

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/model_settings.config", model_settings)
