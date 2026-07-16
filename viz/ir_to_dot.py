"""Static visualization: IR JSON -> Graphviz DOT (+ render SVG).

Two tiers on one page (top-to-bottom, scroll down for detail):
  1. OVERVIEW  — module-rollup pipeline DAG(s): functions collapsed to their file,
     tables attached, one "Pipeline N" cluster per weakly-connected component
     (independent pipelines = separate DAGs). Broad overview.
  2. DETAIL    — the full function-level graph. Deterministic, legend by node.kind (D4).

Usage: python ir_to_dot.py <ir.json> <out.dot>   # then: dot -Tsvg out.dot -o out.svg
"""
import json, sys, html, os, subprocess, re

NODE_STYLE = {
    "function":      ("box",       "#cfe8ff"),
    "method":        ("box",       "#d9f2f2"),
    "module_entry":  ("Mdiamond",  "#ffe0b3"),
    "class":         ("box",       "#e0e0e0"),
    "sql_call":      ("ellipse",   "#f2e2a8"),
    "http_call":     ("hexagon",   "#f8d0da"),
    "http_endpoint": ("hexagon",   "#f8d0da"),
    "table":         ("cylinder",  "#c6f0c6"),
    "view":          ("cylinder",  "#a8e6a8"),
}
EDGE_STYLE = {
    "calls":   ("#333333", "solid",  "1.0"),
    "invokes": ("#888888", "dashed", "1.0"),
    "reads":   ("#1a7f1a", "solid",  "1.6"),
    "writes":  ("#c0392b", "solid",  "1.6"),
    "derives": ("#2c6fbf", "dotted", "1.2"),
    "http":    ("#e67e22", "solid",  "1.2"),
    "maps":    ("#888888", "dashed", "1.0"),
}

def esc(s): return html.escape(str(s)).replace('"', '\\"')

def short(nid):
    if nid.startswith("db::"): return nid[4:]
    if nid.startswith("http::"): return nid[6:][:40]
    if "::" in nid:
        f, q = nid.split("::", 1)
        return f"{f.split('/')[-1]}\\n{q}"
    return nid

# ---- union-find for weakly-connected components ----
def components(items, pairs):
    parent = {x: x for x in items}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[ra] = rb
    for a, b in pairs:
        if a in parent and b in parent: union(a, b)
    groups = {}
    for x in items: groups.setdefault(find(x), []).append(x)
    return [sorted(v) for _, v in sorted(groups.items())]

def build_overview(nodes, edges):
    """Roll functions up to their module (file); return module_calls, module_table (reads/writes), files."""
    file_of = {n["id"]: n.get("file") for n in nodes}
    # sql_call owner file (via invokes) so table access rolls to the owning module
    owner_file = {}
    for e in edges:
        if e["kind"] == "invokes":
            owner_file[e["dst"]] = file_of.get(e["src"])
    tbl_name = {n["id"]: n["id"][4:] for n in nodes if n["kind"] in ("table", "view")}

    mod_calls = set()     # (src_file, dst_file)
    for e in edges:
        if e["kind"] == "calls":
            sf, df = file_of.get(e["src"]), file_of.get(e["dst"])
            if sf and df and sf != df: mod_calls.add((sf, df))
    mod_tbl = set()       # (file, op, table)
    for e in edges:
        if e["kind"] in ("reads", "writes"):
            f = owner_file.get(e["src"])
            t = tbl_name.get(e["dst"])
            if f and t: mod_tbl.add((f, "write" if e["kind"] == "writes" else "read", t))
    files = sorted({f for f in file_of.values() if f})
    return files, sorted(mod_calls), sorted(mod_tbl)

def overview_dot(nodes, edges):
    files, mod_calls, mod_tbl = build_overview(nodes, edges)
    comps = components(files, [(a, b) for a, b in mod_calls])  # weakly-connected = separate pipelines
    ov_tables = sorted({t for _, _, t in mod_tbl})
    L = ["digraph overview {", '  rankdir=TB; compound=true;',
         '  graph [fontname="Helvetica", nodesep=0.35, ranksep=0.6];',
         '  node  [fontname="Helvetica", fontsize=11, style="filled,rounded"];',
         '  edge  [fontname="Helvetica", fontsize=8];']
    for i, comp in enumerate(comps):
        L.append(f'  subgraph cluster_pipe_{i} {{')
        L.append(f'    label="Pipeline {i+1}"; fontsize=13; fontname="Helvetica-Bold"; style="dashed,rounded"; color="#7799bb";')
        for f in comp:
            L.append(f'    "mod::{esc(f)}" [label="{esc(f)}", shape=folder, fillcolor="#dbe9ff"];')
        L.append('  }')
    for t in ov_tables:
        L.append(f'  "ovtbl::{esc(t)}" [label="{esc(t)}", shape=cylinder, fillcolor="#c6f0c6", fontsize=10];')
    for a, b in mod_calls:
        L.append(f'  "mod::{esc(a)}" -> "mod::{esc(b)}" [color="#333333"];')
    for f, op, t in mod_tbl:
        L.append(f'  "mod::{esc(f)}" -> "ovtbl::{esc(t)}" [color="{"#c0392b" if op=="write" else "#1a7f1a"}", penwidth=1.5, label="{op}"];')
    L.append("}")
    return "\n".join(L) + "\n", len(files), len(comps)

def file_of_id(nid):
    if nid.startswith(("db::", "http::")): return None
    return nid.split("::", 1)[0] if "::" in nid else None

def node_line(n):
    shape, fill = NODE_STYLE.get(n["kind"], ("box", "#ffffff"))
    return (f'"{esc(n["id"])}" [label="{esc(short(n["id"]))}", shape={shape}, '
            f'fillcolor="{fill}", tooltip="{esc(n["id"])} [{n["kind"]}]"];')

def detail_dot(nodes, edges):
    kinds = sorted({n["kind"] for n in nodes})
    # bucket function/method/sql_call nodes by their file -> one cluster per module (packs more squarely,
    # so a wide fan wraps into stacked module boxes that fit page width). tables/http stay ungrouped.
    by_file, loose = {}, []
    for n in nodes:
        f = file_of_id(n["id"])
        (by_file.setdefault(f, []).append(n) if f else loose.append(n))
    L = ["digraph detail {", '  rankdir=TB;',
         '  graph [fontname="Helvetica", nodesep=0.25, ranksep=0.55, splines=true, pack=true, packmode="graph"];',
         '  node  [fontname="Helvetica", fontsize=9, style="filled,rounded"];',
         '  edge  [fontname="Helvetica", fontsize=7];']
    for i, f in enumerate(sorted(by_file)):
        L.append(f'  subgraph cluster_f{i} {{')
        L.append(f'    label="{esc(f)}"; fontsize=11; fontname="Helvetica-Bold"; style="rounded"; color="#aab8c8"; bgcolor="#f5f8fc";')
        for n in sorted(by_file[f], key=lambda x: x["id"]):
            L.append("    " + node_line(n))
        L.append('  }')
    for n in loose:
        L.append("  " + node_line(n))
    for e in edges:
        color, style, pw = EDGE_STYLE.get(e["kind"], ("#333333", "solid", "1.0"))
        lbl = e["kind"] if e["kind"] in ("reads", "writes", "derives", "http") else ""
        L.append(f'  "{esc(e["src"])}" -> "{esc(e["dst"])}" [color="{color}", style={style}, penwidth={pw}'
                 + (f', label="{lbl}"' if lbl else "") + '];')
    L.append('  subgraph cluster_legend { label="Legend"; style=dashed; color="#999999";')
    for k in kinds:
        shape, fill = NODE_STYLE.get(k, ("box", "#ffffff"))
        L.append(f'    "lg_{k}" [label="{k}", shape={shape}, fillcolor="{fill}"];')
    L.append('  }\n}')
    return "\n".join(L) + "\n"

_PANZOOM_JS = """
function panzoom(vp){
  const svg=vp.querySelector('svg'); if(!svg) return null;
  const vb=(svg.getAttribute('viewBox')||'0 0 1000 1000').split(/\\s+/).map(Number);
  const W=vb[2], H=vb[3]; svg.removeAttribute('width'); svg.removeAttribute('height');
  svg.setAttribute('width',W); svg.setAttribute('height',H);
  let s=1,x=0,y=0,drag=false,px,py;
  function apply(){svg.style.transform=`translate(${x}px,${y}px) scale(${s})`;}
  function fit(){const r=vp.getBoundingClientRect();s=Math.min(r.width/W,r.height/H)*0.97;x=(r.width-W*s)/2;y=(r.height-H*s)/2;apply();}
  function zoom(f){const r=vp.getBoundingClientRect(),mx=r.width/2,my=r.height/2;const ns=Math.min(40,Math.max(0.02,s*f));x=mx-(mx-x)*(ns/s);y=my-(my-y)*(ns/s);s=ns;apply();}
  vp.addEventListener('wheel',e=>{e.preventDefault();const r=vp.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;const ds=Math.exp(-e.deltaY*0.0015),ns=Math.min(40,Math.max(0.02,s*ds));x=mx-(mx-x)*(ns/s);y=my-(my-y)*(ns/s);s=ns;apply();},{passive:false});
  vp.addEventListener('pointerdown',e=>{drag=true;px=e.clientX-x;py=e.clientY-y;vp.classList.add('grabbing');vp.setPointerCapture(e.pointerId);});
  vp.addEventListener('pointermove',e=>{if(drag){x=e.clientX-px;y=e.clientY-py;apply();}});
  vp.addEventListener('pointerup',()=>{drag=false;vp.classList.remove('grabbing');});
  fit(); return {zoom,fit};
}
function collapsify(vp){
  const svg=vp.querySelector('svg'); if(!svg) return;
  const nodeG={}, out={}, indeg={}, allN=new Set(), edges=[];
  svg.querySelectorAll('g.node').forEach(g=>{const t=g.querySelector('title'); if(!t)return; const id=t.textContent; nodeG[id]=g; allN.add(id); if(!(id in indeg))indeg[id]=0;});
  svg.querySelectorAll('g.edge').forEach(g=>{const t=g.querySelector('title'); if(!t)return; const p=t.textContent.split('->'); if(p.length!=2)return; const a=p[0],b=p[1]; edges.push({g,a,b}); (out[a]=out[a]||[]).push(b); indeg[b]=(indeg[b]||0)+1; if(!(a in indeg))indeg[a]=0;});
  const collapsed=new Set();
  function recompute(){
    const roots=[...allN].filter(n=>(indeg[n]||0)===0);
    const start=roots.length?roots:[...allN];              // fallback if pure cycle
    const seen=new Set(), q=[...start];
    while(q.length){const n=q.shift(); if(seen.has(n))continue; seen.add(n); if(collapsed.has(n))continue; for(const m of (out[n]||[])) if(!seen.has(m)) q.push(m);}
    allN.forEach(id=>{const g=nodeG[id]; g.style.display=seen.has(id)?'':'none'; g.style.opacity=collapsed.has(id)?0.45:1;});
    edges.forEach(({g,a,b})=>{ g.style.display=(seen.has(a)&&seen.has(b)&&!collapsed.has(a))?'':'none'; });
  }
  svg.querySelectorAll('g.node').forEach(g=>{
    g.style.cursor='pointer';
    g.addEventListener('click',e=>{e.stopPropagation(); const t=g.querySelector('title'); if(!t)return; const id=t.textContent; collapsed.has(id)?collapsed.delete(id):collapsed.add(id); recompute();});
  });
}
"""

def render_svg(dot_src):
    """DOT source -> inline SVG markup (strip xml/doctype so it embeds cleanly)."""
    svg = subprocess.run(["dot", "-Tsvg"], input=dot_src, capture_output=True, text=True, check=True).stdout
    svg = re.sub(r'<\?xml.*?\?>', '', svg, flags=re.S)
    svg = re.sub(r'<!DOCTYPE.*?>', '', svg, flags=re.S)
    return svg[svg.index('<svg'):]

def panzoom_page(title, panels):
    """panels = list of {id, label, hint, height, svg}. Returns a self-contained pan/zoom HTML page."""
    nav = "".join(f'<a onclick="document.getElementById(\'{p["id"]}\').scrollIntoView()">{esc(p["label"])}</a>' for p in panels)
    body = []
    for p in panels:
        body.append(f'<section id="{p["id"]}"><h2>{esc(p["label"])}</h2>'
                    f'<p class=hint>{p.get("hint","")}</p>'
                    f'<div class=toolbar><button onclick="PZ[\'{p["id"]}\'].zoom(1.3)">＋</button>'
                    f'<button onclick="PZ[\'{p["id"]}\'].zoom(0.77)">－</button>'
                    f'<button onclick="PZ[\'{p["id"]}\'].fit()">fit</button></div>'
                    f'<div class="vp" id="vp_{p["id"]}" style="height:{p.get("height","80vh")}">{p["svg"]}</div></section>')
    init = ";".join(f'PZ[\'{p["id"]}\']=panzoom(document.getElementById("vp_{p["id"]}")),collapsify(document.getElementById("vp_{p["id"]}"))' for p in panels)
    return (f'<!doctype html><html><head><meta charset=utf-8><title>{esc(title)}</title><style>'
            'body{font-family:Helvetica,Arial,sans-serif;margin:0;background:#fff;color:#222}'
            'header{position:sticky;top:0;background:#2c3e50;color:#fff;padding:10px 18px;z-index:9}'
            'header a{color:#9ecbff;margin-left:14px;text-decoration:none;cursor:pointer}'
            'section{padding:12px 18px;border-bottom:2px solid #eee}h2{margin:4px 0}'
            '.hint{color:#777;font-size:13px;margin:0 0 8px}.toolbar{margin:0 0 6px}'
            '.toolbar button{font-size:13px;padding:3px 9px;margin-right:5px;border:1px solid #bbb;border-radius:4px;background:#fff;cursor:pointer}'
            '.toolbar button:hover{background:#eef}'
            '.vp{width:100%;border:1px solid #ccc;border-radius:6px;overflow:hidden;background:#fafafa;position:relative;cursor:grab;touch-action:none}'
            '.vp.grabbing{cursor:grabbing}.vp svg{position:absolute;top:0;left:0;transform-origin:0 0}'
            '</style></head><body>'
            f'<header><b>{esc(title)}</b> &nbsp; scroll-wheel = zoom · drag = pan {nav}</header>'
            + "".join(body)
            + f'<script>{_PANZOOM_JS}\nconst PZ={{}};{init};'
            'addEventListener("resize",()=>{for(const k in PZ)PZ[k]&&PZ[k].fit();});</script></body></html>')

def main(ir_path, out_prefix):
    ir = json.load(open(ir_path))
    nodes = sorted(ir["nodes"], key=lambda n: n["id"])
    edges = sorted(ir["edges"], key=lambda e: (e["src"], e["dst"], e["kind"]))
    repo = ir.get("meta", {}).get("repo", os.path.basename(out_prefix))

    ov_src, nfiles, ncomp = overview_dot(nodes, edges)
    dt_src = detail_dot(nodes, edges)
    open(f"{out_prefix}.overview.dot", "w").write(ov_src)
    open(f"{out_prefix}.detail.dot", "w").write(dt_src)
    page = panzoom_page(f"code-viz — {repo}", [
        {"id": "ov", "label": "Pipeline overview", "height": "46vh", "svg": render_svg(ov_src),
         "hint": "Module rollup — functions collapsed to their file, grouped into independent pipelines. Green=reads, red=writes. <b>Click a module to collapse its downstream.</b>"},
        {"id": "dt", "label": "Detailed graph", "height": "82vh", "svg": render_svg(dt_src),
         "hint": "Every function, sql_call, and table, boxed by module. Zoom to read. <b>Click a node to collapse its exclusive downstream pathway</b> (click again to expand)."},
    ])
    open(f"{out_prefix}.html", "w").write(page)
    print(f"wrote {out_prefix}.html (self-contained, pan/zoom): overview {nfiles} modules / {ncomp} pipelines, "
          f"detail {len(nodes)} nodes/{len(edges)} edges")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
