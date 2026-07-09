"""
Local web GUI for charts23d. Search a chart, edit/reorder colors with a live
preview, then generate the printable 3MF. Reuses faa/extract/build/generate.

Run:  python -m charts23d.web    (opens http://127.0.0.1:5000)
"""
import base64
import os
import threading

from flask import Flask, request, jsonify, send_from_directory, Response

from . import faa, extract, build, complete, preview, export3mf, cli

app = Flask(__name__)
_LOCK = threading.Lock()          # serialize shapely/matplotlib work
CACHE = cli._cache_dir()
OUTDIR = os.path.abspath(os.environ.get("CHARTS23D_OUT", "charts23d_web_out"))
os.makedirs(OUTDIR, exist_ok=True)

_feats = {}                       # pdf_path -> extracted Features (slow, cached)
_base_plans = {}                  # (pdf_path, bed, nozzle) -> (base_plan, chart)


def _resolve_chart(d):
    """Build a chart dict from a request: a local uploaded PDF, or an FAA lookup."""
    if d.get("pdf_path"):
        return faa.local_chart(d["pdf_path"])
    meta = faa.get_metafile(d["cycle"], CACHE)
    info, recs = faa.find_airport_records(meta, d["ident"])
    rec = next(r for r in recs if r["pdf"] == d["pdf"])
    return faa.fetch_record(d["ident"], CACHE, d["cycle"], info, rec)


def _remapped(d):
    chart = _resolve_chart(d)
    bed = float(d.get("bed") or 256)
    nozzle = float(d.get("nozzle") or 0.2)
    pk = chart["pdf_path"]
    if pk not in _feats:                                  # slow: extract + completeness
        f = extract.extract(pk)
        complete.add_completeness(pk, f)
        _feats[pk] = f
    key = (pk, bed, nozzle)
    if key not in _base_plans:                            # re-plan on bed/nozzle change
        bp = build.plan(_feats[pk], build.Options(
            fit_bed=True, base_mm=1.0, bed_mm=bed, nozzle_mm=nozzle))
        _base_plans[key] = (bp, chart)
    bp, chart = _base_plans[key]
    p = build.remap_plan(bp, d.get("mapping") or {}, d.get("order") or None)
    return bp, chart, p


@app.get("/")
def index():
    return Response(_HTML, mimetype="text/html")


@app.get("/api/cycles")
def cycles():
    out = []
    for i, (code, frm, to) in enumerate(faa.cycle_options(count=2)):
        out.append({"code": code, "label": ("current" if i == 0 else "next"),
                    "from": str(frm), "to": str(to)})
    return jsonify(out)


@app.get("/api/search")
def search():
    cycle = request.args["cycle"]
    q = request.args.get("q", "")
    meta = faa.get_metafile(cycle, CACHE)
    res = faa.search_airports(meta, q) if q else []
    res.sort(key=lambda a: (a["icao"] or a["apt"] or ""))
    return jsonify([{"ident": a["icao"] or a["apt"], "name": a["name"]} for a in res[:300]])


@app.get("/api/charts")
def charts():
    cycle = request.args["cycle"]
    ident = request.args["ident"]
    meta = faa.get_metafile(cycle, CACHE)
    info, recs = faa.find_airport_records(meta, ident)
    recs = [r for r in recs if r["pdf"]]
    types = {}
    for r in recs:
        types.setdefault(r["code"], []).append({"name": r["name"], "pdf": r["pdf"]})
    out = [{"code": c, "label": faa.CHART_CODES.get(c, c), "charts": v} for c, v in types.items()]
    return jsonify({"name": (info or {}).get("name"), "types": out})


def _plan_summary(bp, p):
    return {
        "detected": bp.flat,
        "stack": [g for g in p.order],
        "size_mm": [round(x, 1) for x in p.size_mm],
        "survive": round(p.stats.get("survive_pct", 0), 1) if p.stats else None,
        "filaments": len(p.flat),
    }


@app.post("/api/upload")
def api_upload():
    f = request.files["pdf"]
    dest = os.path.join(OUTDIR, "uploads")
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, os.path.basename(f.filename))
    f.save(path)
    return jsonify({"pdf_path": path, "name": os.path.splitext(os.path.basename(path))[0]})


@app.post("/api/preview")
def api_preview():
    d = request.get_json(force=True)
    with _LOCK:
        bp, chart, p = _remapped(d)
        png = preview.render_bytes(p, title=f"{chart['ident']}  {chart['chart_name']}")
    return jsonify({**_plan_summary(bp, p),
                    "image": "data:image/png;base64," + base64.b64encode(png).decode()})


@app.post("/api/generate")
def api_generate():
    import copy
    import re
    import trimesh
    d = request.get_json(force=True)
    with _LOCK:
        bp, chart, p = _remapped(d)
        style = d.get("style", "min-swaps")
        p.opts = copy.copy(p.opts)                        # don't mutate cached base plan
        p.opts.min_swaps = style == "min-swaps"
        p.opts.layered = style in ("min-swaps", "layered")
        p.opts.base_mm = float(d.get("base", 1.0))

        tag = chart["ident"]
        if chart["chart_code"] not in ("APD", "PDF"):
            tag += "_" + re.sub(r"[^A-Za-z0-9]+", "_", chart["chart_name"]).strip("_")
        tag = re.sub(r"[^A-Za-z0-9_]+", "_", tag).strip("_") or "chart"
        prefix = os.path.join(OUTDIR, tag)

        meshes = build.build_meshes(p)
        build.export(meshes, prefix)                      # per-color STLs + scene .3mf
        colors = {n: p.colors.get(n, "#808080") for n in meshes}
        cpath = prefix + "_colored.3mf"
        summ = export3mf.write_colored_3mf(meshes, colors, cpath, order=[build.BASE] + p.flat)
        # single-color combined STL (whole model, one object)
        stl = prefix + "_combined.stl"
        trimesh.util.concatenate(list(meshes.values())).export(stl)
    return jsonify({"file": os.path.basename(cpath), "stl": os.path.basename(stl),
                    "filaments": summ["n_filaments"],
                    "colors": [colors[n] for n in ([build.BASE] + p.flat) if n in colors]})


@app.get("/download/<path:name>")
def download(name):
    return send_from_directory(OUTDIR, name, as_attachment=True)


def main(port=5000, open_browser=True):
    url = f"http://127.0.0.1:{port}"
    print(f"charts23d GUI -> {url}   (output dir: {OUTDIR})")
    if open_browser:
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True)


_HTML = r"""<!doctype html><html><head><meta charset=utf-8>
<title>Chart &#8594; 3D</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light dark}
body{font:14px/1.4 system-ui,sans-serif;margin:0;display:flex;height:100vh}
#side{width:340px;padding:14px;overflow:auto;border-right:1px solid #8884;box-sizing:border-box}
#main{flex:1;display:flex;align-items:flex-start;justify-content:center;padding:14px;overflow:auto;background:#7771}
h1{font-size:18px;margin:.2em 0 .6em}
h3{margin:1em 0 .3em;font-size:13px;text-transform:uppercase;letter-spacing:.04em;opacity:.7}
select,input,button{font:inherit;padding:5px 7px;border-radius:6px;border:1px solid #8886}
select,input,option{background:#fff;color:#111}
@media (prefers-color-scheme:dark){select,input,option{background:#2b2b2b;color:#eee}}
button{cursor:pointer;background:#3b82f6;color:#fff;border:none}
button.sec{background:#8883;color:inherit}
#results div{padding:5px 7px;border-radius:6px;cursor:pointer}
#results div:hover{background:#3b82f633}
#img{max-width:100%;max-height:96vh;box-shadow:0 2px 12px #0004;background:#fff}
.row{display:flex;align-items:center;gap:6px;margin:4px 0}
.sw{width:26px;height:26px;border-radius:5px;border:1px solid #8886;padding:0}
.grip{cursor:grab;opacity:.5;padding:0 3px}
.small{font-size:12px;opacity:.75}
label{display:flex;gap:5px;align-items:center}
.hidden{display:none}
</style></head><body>
<div id=side>
<h1>Chart &#8594; 3D</h1>
<h3>Upload a PDF (Jeppesen / any vector chart)</h3>
<div class=row><input type=file id=pdf accept="application/pdf" style=flex:1><button class=sec onclick=uploadPdf()>Use</button></div>
<div class=small>— or search the FAA database —</div>
<h3>1 · Cycle</h3><select id=cycle></select>
<h3>2 · Airport</h3>
<div class=row><input id=q placeholder="code or name (e.g. KILM)" style=flex:1><button onclick=doSearch()>Go</button></div>
<div id=results></div>
<div id=chartsec class=hidden>
<h3>3 · Chart</h3>
<select id=ctype onchange=fillCharts()></select>
<select id=chart style="margin-top:6px;width:100%"></select>
<button style="margin-top:8px;width:100%" onclick=loadChart()>Load chart</button>
</div>
<div id=editsec class=hidden>
<h3>4 · Colors (bottom → top)</h3>
<div class=small>Pick each filament color — same color merges. Drag ▤ to reorder. □ = same level as above.</div>
<div id=colors></div>
<h3>Style</h3>
<label><input type=radio name=style value=min-swaps checked> min-swaps (fewest changes)</label>
<label><input type=radio name=style value=layered> layered (solid colors)</label>
<label><input type=radio name=style value=flat> flat relief</label>
<div class=row><span>Base mm</span><input id=base type=number value=1.0 step=0.2 style=width:70px></div>
<h3>Printer</h3>
<select id=printer onchange=setPrinter() style=width:100%>
<option value="256,0.2">Bambu P2S / X1 / P1S / A1 — 256mm · 0.2 nozzle</option>
<option value="256,0.4">Bambu P2S / X1 / P1S / A1 — 256mm · 0.4 nozzle</option>
<option value="320,0.4">Bambu H2D — 320mm · 0.4</option>
<option value="180,0.4">Bambu A1 mini — 180mm · 0.4</option>
<option value="210,0.4">Prusa MK4 / MK3S — 250×210mm · 0.4</option>
<option value="180,0.4">Prusa MINI+ — 180mm · 0.4</option>
<option value="360,0.4">Prusa XL — 360mm · 0.4</option>
<option value="220,0.4">Creality Ender 3 (V2/V3) — 220mm · 0.4</option>
<option value="220,0.4">Creality K1 — 220mm · 0.4</option>
<option value="350,0.4">Voron 2.4 (350) — 350mm · 0.4</option>
<option value="">Custom</option>
</select>
<div class=row><span>Bed mm</span><input id=bed type=number value=256 step=1 style=width:70px oninput=apply()>
<span>Nozzle</span><input id=nozzle type=number value=0.2 step=0.05 style=width:60px oninput=apply()></div>
<div id=info class=small></div>
<button style="margin-top:10px;width:100%" onclick=generate()>Generate</button>
<div id=dl style="margin-top:8px"></div>
</div>
</div>
<div id=main><div id=imgwrap class=small>Pick a chart to begin.</div></div>
<script>
let S={cycle:null,ident:null,pdf:null,colors:[]}; // colors:[{pdf,target,drop,group}]
const $=id=>document.getElementById(id);
async function j(u,o){const r=await fetch(u,o);return r.json()}
(async()=>{const c=await j('/api/cycles');$('cycle').innerHTML=c.map(x=>`<option value=${x.code}>${x.label} — ${x.code} (${x.from} → ${x.to})</option>`).join('')})();
async function doSearch(){const q=$('q').value.trim();if(!q)return;
 const r=await j('/api/search?cycle='+$('cycle').value+'&q='+encodeURIComponent(q));
 $('results').innerHTML=r.map(a=>`<div onclick="pickAirport('${a.ident}')">${a.ident} · ${a.name}</div>`).join('')||'<div class=small>none found</div>';}
$('q').addEventListener('keydown',e=>{if(e.key=='Enter')doSearch()});
async function pickAirport(id){S.ident=id;S.cycle=$('cycle').value;
 const r=await j('/api/charts?cycle='+S.cycle+'&ident='+id);
 $('ctype').innerHTML=r.types.map((t,i)=>`<option value=${i}>${t.label} [${t.code}] (${t.charts.length})</option>`).join('');
 S.types=r.types;fillCharts();$('chartsec').classList.remove('hidden');}
function fillCharts(){const t=S.types[$('ctype').value];
 $('chart').innerHTML=t.charts.map(c=>`<option value="${c.pdf}">${c.name}</option>`).join('');}
async function loadChart(){S.pdf_path=null;S.pdf=$('chart').value;startLoad();}
async function uploadPdf(){const f=$('pdf').files[0];if(!f)return;
 $('imgwrap').innerHTML='Uploading…';const fd=new FormData();fd.append('pdf',f);
 const r=await fetch('/api/upload',{method:'POST',body:fd}).then(x=>x.json());
 S.pdf_path=r.pdf_path;S.ident=r.name;S.pdf=null;startLoad();}
async function startLoad(){$('imgwrap').innerHTML='Loading & analyzing chart… (first load can take a minute)';
 $('editsec').classList.add('hidden');
 const r=await preview({});S.colors=r.detected.map(c=>({pdf:c,target:c,drop:false,group:false}));
 renderColors();$('editsec').classList.remove('hidden');apply();}
function renderColors(){$('colors').innerHTML='';S.colors.forEach((c,i)=>{
 const d=document.createElement('div');d.className='row';d.draggable=true;
 d.ondragstart=e=>e.dataTransfer.setData('i',i);
 d.ondragover=e=>e.preventDefault();
 d.ondrop=e=>{const f=+e.dataTransfer.getData('i');const m=S.colors.splice(f,1)[0];S.colors.splice(i,0,m);renderColors();apply();};
 d.innerHTML=`<span class=grip>▤</span>`;
 const sw=document.createElement('input');sw.type='color';sw.className='sw';sw.value=c.target;
 sw.oninput=()=>{c.target=sw.value;apply()};d.appendChild(sw);
 const lab=document.createElement('span');lab.textContent=c.pdf;lab.className='small';lab.style.flex='1';d.appendChild(lab);
 if(i>0){const g=document.createElement('label');g.title='same level as above';
  g.innerHTML=`<input type=checkbox ${c.group?'checked':''}>▂`;g.querySelector('input').onchange=e=>{c.group=e.target.checked;apply()};d.appendChild(g);}
 const x=document.createElement('button');x.className='sec';x.textContent=c.drop?'＋':'×';x.title='drop to background';
 x.onclick=()=>{c.drop=!c.drop;renderColors();apply()};d.appendChild(x);
 if(c.drop)d.style.opacity=.4;
 $('colors').appendChild(d);});}
function buildParams(){const mapping={},order=[];
 S.colors.forEach(c=>{mapping[c.pdf]=c.drop?null:c.target;});
 S.colors.filter(c=>!c.drop).forEach(c=>{if(c.group&&order.length)order[order.length-1].push(c.target);else order.push([c.target]);});
 const src=S.pdf_path?{pdf_path:S.pdf_path}:{cycle:S.cycle,ident:S.ident,pdf:S.pdf};
 const bed=+(($('bed')||{}).value||256),nozzle=+(($('nozzle')||{}).value||0.2);
 return {...src,mapping,order,bed,nozzle};}
function setPrinter(){const v=$('printer').value;if(!v)return;const a=v.split(',');$('bed').value=a[0];$('nozzle').value=a[1];apply();}
async function preview(extra){const p={...buildParams(),...extra};
 const r=await j('/api/preview',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(p)});
 $('imgwrap').innerHTML=`<img id=img src="${r.image}">`;
 $('info').innerHTML=`Size ${r.size_mm[0]}×${r.size_mm[1]} mm · detail ${r.survive}% · ${r.filaments} filament(s)`;
 return r;}
let T;function apply(){clearTimeout(T);T=setTimeout(()=>preview({}),250);}
async function generate(){$('dl').innerHTML='Building mesh…';
 const style=document.querySelector('input[name=style]:checked').value;
 const p={...buildParams(),style,base:+$('base').value};
 const r=await j('/api/generate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(p)});
 $('dl').innerHTML=`<a href="/download/${r.file}"><button style="width:100%;margin-bottom:6px">⬇ 3MF (color · ${r.filaments} filaments)</button></a>`
  +`<a href="/download/${r.stl}"><button class=sec style=width:100%>⬇ STL (single color)</button></a>`;}
</script></body></html>"""


if __name__ == "__main__":
    main()
