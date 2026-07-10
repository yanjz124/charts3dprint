"""
Interactive wizard: cycle -> airport search (paged) -> chart type -> chart ->
options -> generate. Launched when charts3dprint is run with no airport identifier.
"""
from . import faa, build


def _ask(prompt, default=""):
    try:
        s = input(prompt).strip()
    except EOFError:
        return default
    return s if s else default


def _paginate(items, render, page_size=15, header="results"):
    """Return the selected item, None to quit, or 'SEARCH' to search again."""
    page = 0
    pages = max(1, (len(items) + page_size - 1) // page_size)
    while True:
        page = max(0, min(page, pages - 1))
        start = page * page_size
        print(f"\n{len(items)} {header} (page {page + 1}/{pages}):")
        for i, it in enumerate(items[start:start + page_size]):
            print(f"  {start + i + 1:3d}) {render(it)}")
        cmd = _ask("  #=select, [n]ext, [p]rev, [s]earch, [q]uit: ", "n").lower()
        if cmd == "q":
            return None
        if cmd == "s":
            return "SEARCH"
        if cmd in ("n", ""):
            page += 1
        elif cmd == "p":
            page -= 1
        elif cmd.isdigit() and 0 <= int(cmd) - 1 < len(items):
            return items[int(cmd) - 1]
        else:
            print("  ?")


def _choose_cycle(cache):
    opts = faa.cycle_options(count=2)
    labels = ["current", "next"]
    print("\nCycle:")
    avail = []
    for i, (code, frm, to) in enumerate(opts):
        ok = (i == 0) or faa._url_ok(faa._metafile_url(code))
        avail.append(ok)
        note = "" if ok else "  (not published yet)"
        print(f"  {i + 1}) {labels[i]:7s}  {code}   {frm} -> {to}{note}")
    sel = _ask("Select cycle [1]: ", "1")
    if sel.strip() == "2" and avail[1]:
        return opts[1][0]
    return opts[0][0]


def _choose_airport(meta):
    while True:
        q = _ask("\nAirport code or name (partial ok, blank to quit): ")
        if not q:
            return None
        matches = faa.search_airports(meta, q)
        if not matches:
            print("  none found — try again.")
            continue
        matches.sort(key=lambda a: (a["icao"] or a["apt"] or ""))
        sel = _paginate(matches, lambda a: f"{(a['icao'] or a['apt'] or '?'):5s} {a['name']}",
                        header="airports")
        if sel == "SEARCH":
            continue
        return sel  # dict or None


def _choose_chart(meta, ident):
    info, recs = faa.find_airport_records(meta, ident)
    recs = [r for r in recs if r["pdf"]]
    if not recs:
        print("  no charts for this airport.")
        return info, None
    codes = []
    for r in recs:
        if r["code"] not in codes:
            codes.append(r["code"])
    print("\nChart type:")
    for i, code in enumerate(codes):
        n = sum(1 for r in recs if r["code"] == code)
        print(f"  {i + 1}) {faa.CHART_CODES.get(code, code)} [{code}]  ({n})")
    sel = _ask("Select type [1]: ", "1")
    try:
        code = codes[int(sel) - 1]
    except Exception:
        code = codes[0]
    charts = [r for r in recs if r["code"] == code]
    if len(charts) == 1:
        return info, charts[0]
    rec = _paginate(charts, lambda r: r["name"], header=f"{code} charts")
    return info, (None if rec in (None, "SEARCH") else rec)


def _choose_options():
    fit = _ask("Fit to bed 256mm? [Y/n]: ", "y").lower() != "n"
    width = None
    if not fit:
        try:
            width = float(_ask("Content width mm [200]: ", "200"))
        except ValueError:
            width = 200.0
    print("\nStyle:")
    print("  1) min-swaps  fewest filament changes (best for colored airport diagrams)")
    print("  2) layered    solid colors (best for grayscale approach/SID/STAR)")
    print("  3) flat       single relief height, colors by region")
    style = _ask("Select [1]: ", "1")
    try:
        base = float(_ask("Base thickness mm [1.0]: ", "1.0"))
    except ValueError:
        base = 1.0
    palette = _ask("Palette (blank = all PDF colors; e.g. gray,black): ") or None
    order = _ask("Color order bottom->top (blank = auto; '=' same level; e.g. gray,black): ") or None
    o = build.Options(fit_bed=fit, base_mm=base,
                      min_swaps=(style == "1"), layered=(style in ("1", "2")))
    if width:
        o.width_mm = width
    return o, dict(color=True, palette=palette, order=order)


def run(cache, outdir, generate):
    print("=== charts3dprint — interactive ===")
    cycle = _choose_cycle(cache)
    print(f"\nLoading FAA chart index for cycle {cycle} ...")
    meta = faa.get_metafile(cycle, cache)

    while True:
        airport = _choose_airport(meta)
        if airport is None:
            return 0
        ident = airport["icao"] or airport["apt"]
        print(f"\nSelected: {ident}  {airport['name']}")

        info, rec = _choose_chart(meta, ident)
        if rec is None:
            continue
        print(f"\nChart: {rec['code']}: {rec['name']}  ({rec['pdf']})")

        opts, gkw = _choose_options()
        try:
            chart = faa.fetch_record(ident, cache, cycle, info, rec)
            generate(chart, outdir, opts, **gkw)
        except Exception as e:
            print(f"  ! generation failed: {e}")

        if not _ask("\nMake another? [y/N]: ", "n").lower().startswith("y"):
            return 0
