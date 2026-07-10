"""Command-line entry: identifier -> current FAA chart -> preview + 3MF."""
import argparse
import os
import sys

from . import faa, extract, build, preview, export3mf, complete


def _cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return os.path.join(base, "charts3dprint", "cache")


_NAMED = {"white": (255, 255, 255), "black": (0, 0, 0), "gray": (128, 128, 128),
          "grey": (128, 128, 128), "orange": (230, 140, 0), "brown": (140, 80, 0),
          "tan": (150, 90, 20), "red": (220, 0, 0), "blue": (0, 0, 220),
          "green": (0, 150, 0), "yellow": (240, 220, 0), "magenta": (220, 0, 220),
          "cyan": (0, 200, 200)}


def _resolve_color(tok, present):
    """Map a token (hex, hex-prefix, or color name) to a present color hex."""
    t = tok.strip().lstrip("#").lower()
    if not t:
        return None
    low = {c: c.lstrip("#").lower() for c in present}
    for c, h in low.items():
        if h == t:
            return c
    for c, h in low.items():
        if h.startswith(t):
            return c
    if t in _NAMED:
        tr = _NAMED[t]
        return min(present, key=lambda c: sum(
            (int(c[1 + 2 * i:3 + 2 * i], 16) - tr[i]) ** 2 for i in range(3)))
    return None


def _to_hex(tok):
    """Token (name or hex) -> '#RRGGBB', or None."""
    t = tok.strip().lstrip("#").lower()
    if not t:
        return None
    if t in _NAMED:
        return "#{:02X}{:02X}{:02X}".format(*_NAMED[t])
    if len(t) in (3, 6) and all(ch in "0123456789abcdef" for ch in t):
        if len(t) == 3:
            t = "".join(ch * 2 for ch in t)
        return "#" + t.upper()
    return None


def _parse_palette(s):
    out = []
    for tok in s.split(","):
        h = _to_hex(tok)
        if h and h not in out:
            out.append(h)
    return out


def _parse_order(s, present):
    """'gray=white,black' -> [[grayhex, whitehex_or_skip], [blackhex]] (white skipped
    since it's the base). Bottom -> top."""
    import re
    groups = []
    for part in s.split(","):
        g = []
        for tok in re.split(r"[=+]", part):
            c = _resolve_color(tok, present)
            if c and c not in g:
                g.append(c)
        if g:
            groups.append(g)
    return groups or None


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="charts3dprint",
        description="Convert the current FAA airport diagram to a multicolor relief STL/3MF.",
    )
    ap.add_argument("ident", nargs="?", default=None,
                    help="Airport identifier (ICAO like KILM, or local like ILM). "
                         "Omit to launch the interactive wizard.")
    ap.add_argument("-o", "--outdir", default=".", help="Output directory (default: .)")
    ap.add_argument("--chart", default="APD",
                    help="Chart type: APD (airport diagram, default), IAP (approach), "
                         "DP (SID), STAR (arrival), MIN, ...")
    ap.add_argument("--proc", default=None,
                    help="Procedure name substring to pick one chart when several exist, "
                         'e.g. --chart IAP --proc "Z RWY 6"')
    ap.add_argument("--list", action="store_true", help="List available charts for the airport and exit")
    ap.add_argument("--order", default=None,
                    help="Manual color stack, bottom->top, comma-separated; join colors "
                         "with '=' to put them on the SAME level. Colors by name "
                         "(white/gray/black/orange/...) or hex. E.g. --order \"gray=white,black\"")
    ap.add_argument("--palette", default=None,
                    help="Snap every PDF color to the nearest of THESE filaments (merges "
                         "near-identical tones so it matches what you can load). White is "
                         'the base. E.g. --palette "gray,black" or "gray,tan,black".')
    ap.add_argument("--fit-bed", action="store_true",
                    help="Scale as large as fits the print bed (see --bed)")
    ap.add_argument("--bed", type=float, default=256.0, help="Bed size mm (default 256, Bambu P2S)")
    ap.add_argument("--margin", type=float, default=3.0, help="Border kept inside the bed (mm)")
    ap.add_argument("-w", "--width", type=float, default=200.0,
                    help="Chart content width mm when not using --fit-bed (default 200)")
    ap.add_argument("--nozzle", type=float, default=0.2, help="Nozzle mm; sets min feature (default 0.2)")
    ap.add_argument("--min-line", type=float, default=None,
                    help="Guaranteed min line/feature width mm (default = 2x nozzle, so every "
                         "line is >=2 perimeters and prints reliably with even weight). "
                         "Lower toward the nozzle for finer detail on dense charts.")
    ap.add_argument("--relief", type=float, default=1.2, help="Relief height mm (default 1.2)")
    ap.add_argument("--base", type=float, default=2.0, help="Base plate thickness mm (default 2.0)")
    ap.add_argument("--engrave", action="store_true", help="Recess ink into the plate instead of raising")
    ap.add_argument("--color", action="store_true",
                    help="Also write a pre-colored Bambu 3MF (objects tagged with the "
                         "PDF's real colors; map to your AMS filaments on import)")
    ap.add_argument("--min-swaps", action="store_true",
                    help="Color-by-height slabs: whole print is just #bands filament "
                         "swaps (~0.6g purge). Implies --color.")
    ap.add_argument("--layered", action="store_true",
                    help="Color-by-height: stack colors in Z bands so filament changes "
                         "only 2-3x total (minimal purge waste)")
    ap.add_argument("--band", type=float, default=0.4, help="Layered: thickness of each color band mm (default 0.4)")
    ap.add_argument("--layer-height", type=float, default=0.12, help="Print layer height mm, for color-change layer #s")
    ap.add_argument("--no-complete", action="store_true",
                    help="Skip the raster completeness pass (which recovers ink the vector "
                         "extractor misses, e.g. embedded-font marginalia)")
    ap.add_argument("--complete-dpi", type=int, default=350, help="Completeness raster DPI (default 350)")
    ap.add_argument("--preview-only", action="store_true", help="Write the preview PNG and stop")
    ap.add_argument("--cycle", default=None, help="Force a d-TPP cycle (e.g. 2607 for next cycle) instead of today's")
    ap.add_argument("--pdf", default=None,
                    help="Process a local PDF directly (any vector chart, e.g. Jeppesen) "
                         "instead of an FAA lookup")
    ap.add_argument("--gui", action="store_true", help="Launch the local web GUI")
    ap.add_argument("--cache", default=None, help="Cache dir (default: LOCALAPPDATA/charts3dprint/cache)")
    args = ap.parse_args(argv)

    cache = args.cache or _cache_dir()
    os.makedirs(args.outdir, exist_ok=True)

    if args.gui:
        from . import web
        web.main()
        return 0

    if args.pdf:
        chart = faa.local_chart(os.path.abspath(args.pdf))
        return generate(chart, args.outdir, _opts_from_args(args),
                        color=args.color, palette=args.palette, order=args.order,
                        no_complete=args.no_complete, complete_dpi=args.complete_dpi,
                        preview_only=args.preview_only)

    if not args.ident:
        from . import interactive
        return interactive.run(cache, args.outdir, generate)

    ident = args.ident.strip().upper()

    if args.list:
        cyc, info, recs = faa.list_charts(ident, cache, cycle=args.cycle)
        if info is None:
            print(f"  ! Airport {ident!r} not found (cycle {cyc}).", file=sys.stderr)
            return 2
        print(f"{ident} = {info.get('name')}  (cycle {cyc})")
        for r in recs:
            if r["pdf"]:
                print(f"  {r['code']:5s} {r['name']}")
        return 0

    print(f"Looking up {ident} {args.chart} in the FAA d-TPP database ...")
    try:
        chart = faa.fetch_chart(ident, cache, cycle=args.cycle,
                                chart_code=args.chart, proc=args.proc)
    except LookupError as e:
        print(f"  ! {e}", file=sys.stderr)
        return 2

    ap_name = chart["airport"].get("name") or ident
    frm, to = chart["effective"]
    shown = chart["airport"].get("icao") or chart["airport"].get("apt") or ident
    print(f"  {shown} = {ap_name}")
    print(f"  {chart['chart_code']}: {chart['chart_name']}")
    print(f"  chart {chart['pdf_name']}  cycle {chart['cycle']}  effective {frm} -> {to}")
    print(f"  {chart['pdf_url']}")

    return generate(chart, args.outdir, _opts_from_args(args),
                    color=args.color, palette=args.palette, order=args.order,
                    no_complete=args.no_complete, complete_dpi=args.complete_dpi,
                    preview_only=args.preview_only)


def _opts_from_args(args):
    return build.Options(
        width_mm=args.width, fit_bed=args.fit_bed, bed_mm=args.bed, margin_mm=args.margin,
        nozzle_mm=args.nozzle, min_line_mm=args.min_line,
        relief_mm=args.relief, base_mm=args.base, engrave=args.engrave,
        layered=args.layered or args.min_swaps, min_swaps=args.min_swaps,
        band_mm=args.band, layer_h=args.layer_height,
    )


def generate(chart, outdir, opts, *, color=False, palette=None, order=None,
             no_complete=False, complete_dpi=350, preview_only=False):
    """Shared pipeline: chart + options -> preview + STL/3MF. Used by the flag CLI
    and the interactive wizard."""
    import re as _re
    do_color = color or opts.min_swaps
    ident = (chart["airport"].get("icao") or chart["airport"].get("apt")
             or chart["ident"]).upper()

    feats = extract.load_features(chart["pdf_path"], do_complete=not no_complete)

    if palette:
        pal = _parse_palette(palette)
        if not pal:
            print(f"  ! palette parsed no colors from {palette!r}", file=sys.stderr)
            return 2
        extract.quantize_to_palette(feats, pal)
        print(f"Quantized to palette: {'  '.join(pal)}")

    if order:
        og = _parse_order(order, feats.colors)
        if not og:
            print(f"  ! order matched no colors; present: {sorted(feats.colors)}", file=sys.stderr)
            return 2
        opts.order = og

    p = build.plan(feats, opts)

    w, h = p.size_mm
    print(f"\nPrint size: {w:.1f} x {h:.1f} mm  (scale {p.scale:.3f} mm/pt)")
    if p.stats:
        print(f"Detail: {p.stats['survive_pct']:.1f}% survives a {p.stats['nozzle_mm']}mm nozzle")
    print("Colors bottom->top (base white, then): "
          + " -> ".join("+".join(g) for g in p.order))
    if opts.fit_bed:
        print(f"Fit to {opts.bed_mm:.0f}mm bed with {opts.margin_mm:.0f}mm margin.")

    tag = ident
    if chart["chart_code"] not in ("APD", "PDF"):
        tag = ident + "_" + _re.sub(r"[^A-Za-z0-9]+", "_", chart["chart_name"]).strip("_")
    prefix = os.path.join(outdir, tag)
    title = f"{ident}  {chart['chart_name']}  (cycle {chart['cycle']})"
    png = preview.render(p, prefix + "_preview.png", title=title)
    print(f"\nPreview: {png}")
    if preview_only:
        return 0

    print("Building meshes ...")
    meshes = build.build_meshes(p)
    three, written = build.export(meshes, prefix)
    for f in written:
        print(f"  wrote {f}")

    if do_color:
        colors = {n: p.colors.get(n, "#808080") for n in meshes}
        cpath = prefix + "_colored.3mf"
        summ = export3mf.write_colored_3mf(meshes, colors, cpath, order=[build.BASE] + p.flat)
        print(f"  wrote {cpath}  (pre-colored, {summ['n_filaments']} filaments)")
        print("  filaments = the PDF's own colors — map each to the closest in your AMS:")
        for n, slot in sorted(summ["filaments"].items(), key=lambda kv: kv[1]):
            print(f"     filament {slot}: {colors[n]}")

    if opts.min_swaps:
        nb = len(p.flat)
        print(f"  min-swaps: whole print is ~{nb} filament changes total (~{0.15*nb:.1f}g purge)")

    if opts.layered and not opts.min_swaps and not do_color:
        changes, total_h = build.color_change_plan(p)
        print(f"\nLayered color-by-height: model is {total_h:.2f} mm tall.")
        print(f"Filament changes: {len(changes)} total (vs. hundreds for paint-by-XY).")
        print("In Bambu Studio, add a color change at each height below:")
        for c in changes:
            print(f"   Z={c['at_z_mm']:.2f}mm  (layer {c['layer']})  {c['from']} -> {c['to']}")

    if do_color:
        print(f"\nDone. Open {prefix}_colored.3mf in Bambu Studio — it opens pre-colored;")
        print("map each filament to the closest color in your AMS.")
    else:
        print(f"\nDone. Open {three} in Bambu Studio; each part is a separate object.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
