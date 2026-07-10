"""
FAA d-TPP chart lookup.

Resolves the current 28-day chart cycle from the date, downloads the official
FAA d-TPP metafile, and finds an airport's Airport Diagram (APD) PDF.

Data source (public, no key): https://aeronav.faa.gov/d-tpp/<cycle>/...
Cycle codes are YYNN (year, ordinal-in-year). Cycles are exactly 28 days,
effective on Thursdays, aligned to AIRAC.
"""
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta

# Verified anchor: d-TPP cycle 2606 is effective 2026-06-11.
_EPOCH = date(2026, 6, 11)
_UA = "charts3dprint/0.1 (+https://aeronav.faa.gov)"
BASE = "https://aeronav.faa.gov/d-tpp"


def _eff_date(k):
    return _EPOCH + timedelta(days=28 * k)


def _cycle_code(k):
    d = _eff_date(k)
    y = d.year
    k0 = k
    while _eff_date(k0 - 1).year == y:
        k0 -= 1
    return "%02d%02d" % (y % 100, k - k0 + 1)


def current_cycle(today=None):
    """Return (cycle_code, from_date, to_date) for the cycle covering `today`."""
    today = today or date.today()
    k = (today - _EPOCH).days // 28
    return _cycle_code(k), _eff_date(k), _eff_date(k + 1)


def cycle_options(today=None, count=2):
    """Return [(code, from, to), ...] for the current cycle and the next few."""
    today = today or date.today()
    k = (today - _EPOCH).days // 28
    return [(_cycle_code(k + i), _eff_date(k + i), _eff_date(k + i + 1))
            for i in range(count)]


def search_airports(metafile_path, query):
    """Airports whose ICAO/local ident or name contains `query` (case-insensitive)."""
    q = _norm(query)
    out = []
    for _, elem in ET.iterparse(metafile_path, events=("end",)):
        if elem.tag != "airport_name":
            continue
        icao = _norm(elem.get("icao_ident"))
        apt = _norm(elem.get("apt_ident"))
        name = elem.get("ID") or ""
        if q in icao or q in apt or q in name.upper():
            out.append({"icao": icao or None, "apt": apt or None, "name": name})
        elem.clear()
    return out


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        f.write(r.read())


def _metafile_url(cycle):
    return f"{BASE}/{cycle}/xml_data/d-TPP_Metafile.xml"


def get_metafile(cycle, cache_dir):
    """Download (and cache) the metafile for a cycle. Returns local path."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"d-TPP_Metafile_{cycle}.xml")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        _download(_metafile_url(cycle), path)
    return path


def _url_ok(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def resolve_cycle(today=None, cache_dir="."):
    """
    Determine the live cycle covering today, self-healing against the anchor.
    Returns the cycle code whose metafile confirms today is in range.
    """
    today = today or date.today()
    base_k = (today - _EPOCH).days // 28
    for dk in (0, 1, -1, 2, -2):
        code = _cycle_code(base_k + dk)
        if _url_ok(_metafile_url(code)):
            path = get_metafile(code, cache_dir)
            frm, to = _read_cycle_dates(path)
            if frm and to and frm <= today < to:
                return code
    # fallback: just use the computed current cycle
    return current_cycle(today)[0]


_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{2})")


def _parse_edate(s):
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    mm, dd, yy = (int(g) for g in m.groups())
    return date(2000 + yy, mm, dd)


def _read_cycle_dates(metafile_path):
    with open(metafile_path, "rb") as f:
        head = f.read(400).decode("utf-8", "replace")
    m = re.search(r'from_edate="([^"]*)"\s+to_edate="([^"]*)"', head)
    if not m:
        return None, None
    return _parse_edate(m.group(1)), _parse_edate(m.group(2))


def _norm(s):
    return (s or "").strip().upper()


def find_airport_records(metafile_path, ident):
    """
    Return (airport_info, [records]) for an identifier, or (None, None).
    Accepts ICAO (KILM) or FAA/local ident (ILM), case-insensitive.
    Each record: {"code", "name", "pdf"}.
    """
    want = _norm(ident)
    want_alt = want[1:] if len(want) == 4 and want[0] == "K" else "K" + want

    for _, elem in ET.iterparse(metafile_path, events=("end",)):
        if elem.tag != "airport_name":
            continue
        icao = _norm(elem.get("icao_ident"))
        apt = _norm(elem.get("apt_ident"))
        if want in (icao, apt) or want_alt in (icao, apt):
            info = {"name": elem.get("ID"), "icao": icao or None,
                    "apt": apt or None, "alnum": elem.get("alnum")}
            recs = [{"code": _norm(r.findtext("chart_code")),
                     "name": (r.findtext("chart_name") or "").strip(),
                     "pdf": (r.findtext("pdf_name") or "").strip()}
                    for r in elem.findall("record")]
            elem.clear()
            return info, recs
        elem.clear()
    return None, None


# Human-friendly chart-type groups.
CHART_CODES = {"APD": "Airport Diagram", "IAP": "Approach plate",
               "DP": "Departure (SID)", "ODP": "Obstacle departure",
               "STAR": "Arrival (STAR)", "STR": "Arrival (STAR)",
               "MIN": "Takeoff/alternate minimums", "LAH": "Land and hold short",
               "HOT": "Hot spot", "DAU": "Diverse/alternate"}


def local_chart(pdf_path):
    """Wrap any local PDF (e.g. a Jeppesen chart) in the same dict shape as
    fetch_chart, so it flows through the exact same pipeline."""
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    return {
        "ident": (name.upper() or "PDF"), "cycle": "-", "effective": (None, None),
        "pdf_name": os.path.basename(pdf_path), "pdf_url": pdf_path,
        "pdf_path": pdf_path, "airport": {"name": name, "icao": None, "apt": None},
        "chart_code": "PDF", "chart_name": name,
    }


def fetch_record(ident, cache_dir, cycle, info, rec):
    """Download the PDF for an exact record (from find_airport_records) and return
    the same dict shape as fetch_chart. Avoids ambiguous name matching."""
    pdf_url = f"{BASE}/{cycle}/{rec['pdf']}"
    pdf_path = os.path.join(cache_dir, f"{cycle}_{rec['pdf']}")
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        _download(pdf_url, pdf_path)
    frm, to = _read_cycle_dates(get_metafile(cycle, cache_dir))
    return {
        "ident": ident, "cycle": cycle, "effective": (frm, to),
        "pdf_name": rec["pdf"], "pdf_url": pdf_url, "pdf_path": pdf_path,
        "airport": info, "chart_code": rec["code"], "chart_name": rec["name"],
    }


def list_charts(ident, cache_dir, cycle=None, today=None):
    """Return (airport_info, [records]) for an identifier."""
    os.makedirs(cache_dir, exist_ok=True)
    if not cycle:
        cycle = resolve_cycle(today=today, cache_dir=cache_dir)
    return (cycle,) + find_airport_records(get_metafile(cycle, cache_dir), ident)


def fetch_chart(ident, cache_dir, today=None, cycle=None, chart_code="APD", proc=None):
    """
    High-level: identifier -> local PDF path of a chart.
    chart_code: APD (airport diagram, default), IAP, DP, STAR/STR, ...
    proc: substring to match the chart name when several exist (e.g. "Z RWY 6").
    Pass `cycle` (e.g. "2607") to force a specific cycle. Returns dict w/ pdf path.
    """
    os.makedirs(cache_dir, exist_ok=True)
    if not cycle:
        cycle = resolve_cycle(today=today, cache_dir=cache_dir)
    meta = get_metafile(cycle, cache_dir)
    info, recs = find_airport_records(meta, ident)
    if info is None:
        raise LookupError(f"Airport {ident!r} not found in FAA d-TPP cycle {cycle}.")

    code = _norm(chart_code)
    # STAR appears as "STR" in the metafile
    codes = {"STAR", "STR"} if code in ("STAR", "STR") else {code}
    cands = [r for r in recs if r["code"] in codes and r["pdf"]]
    if proc:
        pl = _norm(proc)
        cands = [r for r in cands if pl in r["name"].upper()]
    if not cands:
        avail = sorted({r["code"] for r in recs if r["pdf"]})
        hint = f" matching {proc!r}" if proc else ""
        raise LookupError(
            f"No {code} chart{hint} for {ident!r}. Available types: {', '.join(avail)}.")
    if len(cands) > 1:
        opts = "\n".join(f"    --proc \"{r['name']}\"" for r in cands[:25])
        raise LookupError(
            f"{len(cands)} {code} charts for {ident!r}; narrow with --proc:\n{opts}")

    rec = cands[0]
    pdf_name = rec["pdf"]
    pdf_url = f"{BASE}/{cycle}/{pdf_name}"
    pdf_path = os.path.join(cache_dir, f"{cycle}_{pdf_name}")
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) == 0:
        _download(pdf_url, pdf_path)
    frm, to = _read_cycle_dates(meta)
    return {
        "ident": ident,
        "cycle": cycle,
        "effective": (frm, to),
        "pdf_name": pdf_name,
        "pdf_url": pdf_url,
        "pdf_path": pdf_path,
        "airport": info,
        "chart_code": rec["code"],
        "chart_name": rec["name"],
    }
