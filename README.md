# charts23d

**Turn aviation charts into multicolor 3D prints.** Point it at an FAA airport
diagram / approach plate / SID / STAR (or upload any vector PDF, e.g. Jeppesen),
edit the colors, and get a printable **STL / 3MF** — pre-colored for a multi-material
printer (Bambu AMS, etc.).

Aviation charts are *vector* PDFs, so the linework extrudes crisply instead of
looking like a muddy heightmap. Colors come straight from the PDF; you remap them
to the filaments you actually have.

## Install

Requires **Python 3.9+**.

```bash
git clone https://github.com/yanjz124/charts23d
cd charts23d
pip install .          # installs the `charts23d` command
```

or, without installing the command:

```bash
pip install -r requirements.txt
python -m charts23d ...
```

## Use

### Web GUI (easiest — see the colors)
```bash
charts23d --gui                 # opens http://127.0.0.1:5000
```
Search a chart **or upload a PDF**, then pick each filament color with a **live
preview** (same color merges; drag to reorder; □ = same level; × = drop to
background), choose your **printer / bed / nozzle** and style, and download the
STL or the pre-colored 3MF.

### Interactive wizard (terminal)
```bash
charts23d                       # guided: cycle -> airport -> chart type -> chart -> options
```

### One-liners
```bash
# Airport diagram, filling a 256 mm bed, pre-colored 3MF (min filament swaps):
charts23d KATL --fit-bed --min-swaps -o out

# Next d-TPP cycle:
charts23d KCLT --fit-bed --min-swaps --cycle 2607 -o out

# An approach plate, quantized to gray+black (grayscale chart):
charts23d ILM --chart IAP --proc "ILS Z RWY 06" --layered --palette "gray,black" -o out

# Any local PDF (Jeppesen, etc.):
charts23d --pdf mychart.pdf --fit-bed --min-swaps --palette "gray,black" -o out

# List all charts for an airport:
charts23d KATL --list
```

## Printers

`--fit-bed` scales the chart to fill your bed. Set your machine with `--bed` (mm)
and `--nozzle` (mm), or pick a preset in the GUI. Both matter:

- **Bed size** sets the maximum print size.
- **Nozzle** sets the finest detail: a **0.2 mm** nozzle resolves nearly everything
  (fine text, hatching); a **0.4 mm** nozzle loses the smallest labels. The tool
  reports what % of detail survives your nozzle.
- **Multi-material (AMS/MMU)** lets you print the pre-colored 3MF directly; a
  single-extruder printer can still print the combined **STL** in one color.

## Colors & style

- **Colors are the PDF's own.** In the GUI you remap each to a filament; on the CLI
  use `--palette "gray,black"` to snap everything to the filaments you have
  (near-identical tones merge). `--order` sets the stack (bottom→top; `=` = same level).
- **Style:**
  - `--min-swaps` — colors stacked by height, ~2–3 filament changes total. Best for
    the colored **airport diagrams**.
  - `--layered` — each color is a solid full-height column (marks stay truly black).
    Best for grayscale **approach / SID / STAR** charts.
  - flat — single relief height.

## How it works

PyMuPDF reads the vector paths + colors → shapely unions/offsets them (min line
width, letter-counter holes, white carve-outs) → manifold3d extrudes each color to
a watertight solid → exported as per-color STL + a pre-colored Bambu 3MF. A raster
"completeness" pass recovers any embedded-font text the vectors miss.

## Data & license

FAA charts are public-domain, fetched from the official d-TPP at
`aeronav.faa.gov`. MIT licensed. Not for navigation — decorative/reference use only.
