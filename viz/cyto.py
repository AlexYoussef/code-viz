"""Interactive visualization: IR JSON -> self-contained Cytoscape.js HTML.

A real graph engine (Cytoscape.js + dagre) replacing the static Graphviz-SVG
viewer, so pathways are *interactively* collapsible:
  - click a regular node   -> fold its exclusive successor subtree (everything
                              downstream reachable ONLY through it), re-lays out.
  - click a module compound -> fold the whole file/module into one box
                              (cytoscape-expand-collapse), re-lays out.
Plus: dagre DAG layout (top->bottom), pan+zoom, style-by-kind (D4 legend
colors, reused from ir_to_dot.py), edge-kind show/hide toggles, id-substring
search (dim non-matches), full-id hover tooltip, and a legend.

Fully offline: the four vendored libs in viz/vendor/ are INLINED into the HTML,
so it opens from file:// with no network.

Usage: python viz/cyto.py <ir.json> <out.html>
"""
import json, sys, os, html

# --- reused from ir_to_dot.py: node shape+fill and edge color by kind (D4) ---
# cytoscape shapes: round-rectangle | ellipse | barrel | hexagon | diamond
NODE_STYLE = {
    "function":      ("round-rectangle", "#cfe8ff"),
    "method":        ("round-rectangle", "#d9f2f2"),
    "module_entry":  ("diamond",         "#ffe0b3"),
    "class":         ("round-rectangle", "#e0e0e0"),
    "sql_call":      ("ellipse",         "#f2e2a8"),
    "llm_call":      ("diamond",         "#d9b3ff"),
    "llm_endpoint":  ("hexagon",         "#b98be0"),
    "http_call":     ("hexagon",         "#f8d0da"),
    "http_endpoint": ("hexagon",         "#f8d0da"),
    "table":         ("barrel",          "#c6f0c6"),
    "view":          ("barrel",          "#a8e6a8"),
}
EDGE_STYLE = {
    "calls":   ("#333333", "solid"),
    "invokes": ("#888888", "dashed"),
    "reads":   ("#1a7f1a", "solid"),
    "writes":  ("#c0392b", "solid"),
    "prompts": ("#8e44ad", "solid"),
    "derives": ("#2c6fbf", "dotted"),
    "http":    ("#e67e22", "solid"),
    "maps":    ("#888888", "dashed"),
}
VENDOR = ["dagre.min.js", "cytoscape.min.js",
          "cytoscape-dagre.min.js", "cytoscape-expand-collapse.min.js"]


def short(nid):
    """Compact label = qualname (short); tables/http show their bare name."""
    if nid.startswith("db::"):   return nid[4:]
    if nid.startswith("llm::"):  return nid[5:]
    if nid.startswith("http::"): return nid[6:][:40]
    if "::" in nid:
        f, q = nid.split("::", 1)
        return q  # qualname only; the module box already shows the file
    return nid


def file_of_id(nid):
    if nid.startswith(("db::", "llm::", "http::")): return None
    return nid.split("::", 1)[0] if "::" in nid else None


def build_elements(nodes, edges):
    """IR -> sorted cytoscape elements (module compounds + real nodes + edges)."""
    els = []
    files = sorted({f for n in nodes if (f := file_of_id(n["id"]))})
    # module compound parents (one per file) — collapsible via expand-collapse
    for f in files:
        els.append({"data": {"id": f"mod::{f}", "label": f, "kind": "module",
                             "isModule": 1}})
    # real nodes, parented into their module compound
    for n in sorted(nodes, key=lambda x: x["id"]):
        f = file_of_id(n["id"])
        d = {"id": n["id"], "label": short(n["id"]), "full": n["id"],
             "kind": n["kind"]}
        if f:
            d["parent"] = f"mod::{f}"
        els.append({"data": d})
    # edges (skip module compounds; those are structural parents, not edges)
    for e in sorted(edges, key=lambda e: (e["src"], e["kind"], e["dst"])):
        els.append({"data": {
            "id": f'{e["src"]}|{e["kind"]}|{e["dst"]}',
            "source": e["src"], "target": e["dst"], "kind": e["kind"]}})
    return els


def build_elements_diff(old, new):
    """Two IRs -> union cytoscape elements, each tagged data.status = added|removed|same."""
    on = {n["id"]: n for n in old["nodes"]}; nn = {n["id"]: n for n in new["nodes"]}
    oe = {(e["src"], e["dst"], e["kind"]) for e in old["edges"]}
    ne = {(e["src"], e["dst"], e["kind"]) for e in new["edges"]}
    alln = {**on, **nn}
    def nstat(i): return "added" if i in nn and i not in on else "removed" if i in on and i not in nn else "same"
    def estat(k): return "added" if k in ne and k not in oe else "removed" if k in oe and k not in ne else "same"
    els = []
    files = sorted({f for i in alln if (f := file_of_id(i))})
    childstat = {}
    for i in alln:
        f = file_of_id(i)
        if f: childstat.setdefault(f, set()).add(nstat(i))
    for f in files:
        ss = childstat.get(f, {"same"})
        mstat = "added" if ss == {"added"} else "removed" if ss == {"removed"} else "same"
        els.append({"data": {"id": f"mod::{f}", "label": f, "kind": "module", "isModule": 1, "status": mstat}})
    for i in sorted(alln):
        n = alln[i]; f = file_of_id(i)
        d = {"id": i, "label": short(i), "full": i, "kind": n["kind"], "status": nstat(i)}
        if f: d["parent"] = f"mod::{f}"
        els.append({"data": d})
    for (s, t, kind) in sorted(oe | ne):
        els.append({"data": {"id": f"{s}|{kind}|{t}", "source": s, "target": t,
                             "kind": kind, "status": estat((s, t, kind))}})
    add_n = sum(1 for i in alln if nstat(i) == "added"); del_n = sum(1 for i in alln if nstat(i) == "removed")
    add_e = sum(1 for k in (oe | ne) if estat(k) == "added"); del_e = sum(1 for k in (oe | ne) if estat(k) == "removed")
    return els, (add_n, del_n, add_e, del_e)


def cy_style():
    """Cytoscape stylesheet: fill+shape by node kind, color+dash by edge kind."""
    S = [
        {"selector": "node", "style": {
            "label": "data(label)", "font-size": 9, "text-valign": "center",
            "text-halign": "center", "color": "#1a1a1a",
            "text-wrap": "wrap", "text-max-width": 120,
            "border-width": 1, "border-color": "#8a99a8",
            "width": "label", "height": 22, "padding": "6px",
            "shape": "round-rectangle", "background-color": "#ffffff"}},
        # module compound boxes
        {"selector": "node[?isModule]", "style": {
            "label": "data(label)", "font-size": 11, "font-weight": "bold",
            "text-valign": "top", "text-halign": "center",
            "shape": "round-rectangle", "background-color": "#f5f8fc",
            "background-opacity": 0.6, "border-color": "#aab8c8",
            "border-width": 1.5, "color": "#3a4a5a", "padding": "10px"}},
    ]
    for kind, (shape, fill) in sorted(NODE_STYLE.items()):
        S.append({"selector": f'node[kind = "{kind}"]', "style": {
            "shape": shape, "background-color": fill}})
    S += [
        {"selector": "edge", "style": {
            "width": 1.4, "line-color": "#333", "target-arrow-color": "#333",
            "target-arrow-shape": "triangle", "arrow-scale": 0.8,
            "curve-style": "bezier"}},
    ]
    for kind, (color, dash) in sorted(EDGE_STYLE.items()):
        st = {"line-color": color, "target-arrow-color": color}
        if kind in ("reads", "writes", "prompts"): st["width"] = 2.0
        if dash == "dashed": st["line-style"] = "dashed"
        if dash == "dotted": st["line-style"] = "dotted"
        S.append({"selector": f'edge[kind = "{kind}"]', "style": st})
    # collapsed-compound meta node (expand-collapse)
    S += [
        {"selector": "node.cy-expand-collapse-collapsed-node", "style": {
            "shape": "round-rectangle", "background-color": "#dbe9ff",
            "border-color": "#7799bb", "border-width": 2}},
        # subtree-collapsed marker + dimming/highlight classes
        {"selector": "node.collapsed-root", "style": {
            "border-color": "#c0392b", "border-width": 3,
            "background-blacken": -0.1}},
        {"selector": ".dim", "style": {"opacity": 0.12}},
        {"selector": "node.match", "style": {
            "border-color": "#e67e22", "border-width": 4}},
        {"selector": ".hidden", "style": {"display": "none"}},
        {"selector": ".changedhidden", "style": {"display": "none"}},
        # ---- diff status (present only in diff mode; overrides kind coloring) ----
        {"selector": 'node[status = "added"]', "style": {
            "border-color": "#1a7f1a", "border-width": 3, "background-color": "#b7f0b7"}},
        {"selector": 'node[status = "removed"]', "style": {
            "border-color": "#c0392b", "border-width": 3, "border-style": "dashed",
            "background-color": "#f4b7b7", "opacity": 0.8}},
        {"selector": 'node[status = "same"]', "style": {
            "background-color": "#ededed", "border-color": "#bbb", "color": "#8a8a8a"}},
        {"selector": 'edge[status = "added"]', "style": {
            "line-color": "#1a7f1a", "target-arrow-color": "#1a7f1a", "width": 2.4}},
        {"selector": 'edge[status = "removed"]', "style": {
            "line-color": "#c0392b", "target-arrow-color": "#c0392b", "width": 2.4, "line-style": "dashed"}},
        {"selector": 'edge[status = "same"]', "style": {
            "line-color": "#d3d3d3", "target-arrow-color": "#d3d3d3", "width": 0.7}},
    ]
    return S


# The interactive app logic (kind selectors already baked into style). Uses the
# globals cytoscape, cytoscapeDagre, cytoscapeExpandCollapse, plus ELEMENTS/STYLE.
APP_JS = r"""
cytoscape.use(cytoscapeDagre);
cytoscape.use(cytoscapeExpandCollapse);

var LAYOUT = {name:'dagre', rankDir:'TB', nodeSep:22, rankSep:48,
              edgeSep:8, animate:false, fit:true, padding:20};

var cy = cytoscape({
  container: document.getElementById('cy'),
  elements: ELEMENTS,
  style: STYLE,
  wheelSensitivity: 0.25,
  layout: LAYOUT
});

var api = cy.expandCollapse({
  layoutBy: LAYOUT,
  fisheye: false, animate: false, undoable: false,
  cueEnabled: true, expandCollapseCuePosition: 'top-left'
});

function runLayout(){ cy.layout(LAYOUT).run(); }

/* ---- subtree collapse: hide a node's EXCLUSIVE downstream ---- */
var collapsedRoots = new Set();
// dependency edges (exclude structural compound-parent relationships)
function depEdges(){ return cy.edges().filter(function(e){ return !e.data('isModule'); }); }

function recomputeSubtrees(){
  // reachability from graph roots over dep edges; stop descent at collapsed roots
  var real = cy.nodes().filter(function(n){ return !n.data('isModule'); });
  var indeg = {}; real.forEach(function(n){ indeg[n.id()] = 0; });
  var outMap = {};
  depEdges().forEach(function(e){
    var s = e.data('source'), t = e.data('target');
    if(!(s in indeg) || !(t in indeg)) return;
    (outMap[s] = outMap[s] || []).push(t);
    indeg[t] = (indeg[t]||0) + 1;
  });
  var roots = real.filter(function(n){ return indeg[n.id()] === 0; }).map(function(n){ return n.id(); });
  if(roots.length === 0) roots = real.map(function(n){ return n.id(); }); // pure-cycle fallback
  var seen = new Set(), q = roots.slice();
  while(q.length){
    var n = q.shift();
    if(seen.has(n)) continue; seen.add(n);
    if(collapsedRoots.has(n)) continue;            // don't descend past a collapsed root
    (outMap[n]||[]).forEach(function(m){ if(!seen.has(m)) q.push(m); });
  }
  real.forEach(function(n){
    var id = n.id();
    if(seen.has(id)){ n.removeClass('hidden'); }
    else { n.addClass('hidden'); }
    n.toggleClass('collapsed-root', collapsedRoots.has(id));
  });
  runLayout();
}

/* ---- interactions ---- */
cy.on('tap', 'node', function(evt){
  var n = evt.target;
  if(n.data('isModule')){
    // module compound: fold/unfold whole file
    if(api.isCollapsible(n)) api.collapse(n);
    else if(api.isExpandable(n)) api.expand(n);
    return;
  }
  var id = n.id();
  if(collapsedRoots.has(id)) collapsedRoots.delete(id);
  else collapsedRoots.add(id);
  recomputeSubtrees();
});

/* ---- hover tooltip (full id) ---- */
var tip = document.getElementById('tip');
cy.on('mouseover', 'node', function(evt){
  var n = evt.target;
  tip.textContent = n.data('full') || n.data('label');
  tip.style.display = 'block';
});
cy.on('mousemove', 'node', function(evt){
  var e = evt.originalEvent;
  tip.style.left = (e.clientX + 12) + 'px';
  tip.style.top  = (e.clientY + 12) + 'px';
});
cy.on('mouseout', 'node', function(){ tip.style.display = 'none'; });

/* ---- toolbar ---- */
document.getElementById('fit').onclick   = function(){ cy.fit(null, 20); };
document.getElementById('zin').onclick   = function(){ cy.zoom(cy.zoom()*1.3); };
document.getElementById('zout').onclick  = function(){ cy.zoom(cy.zoom()/1.3); };

document.getElementById('collapseMods').onclick = function(){
  var mods = cy.nodes().filter(function(n){ return n.data('isModule') && api.isCollapsible(n); });
  api.collapse(mods);
};
document.getElementById('expandAll').onclick = function(){
  api.expandAll();
  collapsedRoots.clear();
  recomputeSubtrees();
};

/* ---- search: dim non-matches, highlight matches ---- */
var search = document.getElementById('search');
search.oninput = function(){
  var q = search.value.trim().toLowerCase();
  cy.batch(function(){
    if(!q){ cy.elements().removeClass('dim'); cy.nodes().removeClass('match'); return; }
    var matches = cy.nodes().filter(function(n){
      return !n.data('isModule') && n.id().toLowerCase().indexOf(q) !== -1; });
    cy.elements().addClass('dim');
    cy.nodes().removeClass('match');
    matches.removeClass('dim').addClass('match');
    matches.connectedEdges().removeClass('dim');
    matches.parents().removeClass('dim');
  });
  if(q){ var m = cy.nodes('.match'); if(m.length) cy.fit(m, 60); }
};

/* ---- edge-kind toggles ---- */
['calls','reads','writes','prompts','invokes'].forEach(function(k){
  var cb = document.getElementById('ek_'+k);
  if(!cb) return;
  cb.onchange = function(){
    var es = cy.edges('[kind = "'+k+'"]');
    if(cb.checked) es.removeClass('hidden'); else es.addClass('hidden');
  };
});

/* ---- diff-only controls (present only when rendered in diff mode) ---- */
var changedOnly = document.getElementById('changedOnly');
if(changedOnly) changedOnly.onchange = function(){
  var same = cy.nodes('[status = "same"][!isModule]').add(cy.edges('[status = "same"]'));
  if(changedOnly.checked) same.addClass('changedhidden'); else same.removeClass('changedhidden');
};
var collapseUnchanged = document.getElementById('collapseUnchanged');
if(collapseUnchanged) collapseUnchanged.onclick = function(){
  var mods = cy.nodes('[status = "same"]').filter(function(n){ return n.data('isModule') && api.isCollapsible(n); });
  api.collapse(mods);
};

recomputeSubtrees();  // initial reachability pass (nothing collapsed => all shown)
cy.fit(null, 20);
window.__cy = cy; window.__api = api;  // exposed for headless verification
"""


def esc(s): return html.escape(str(s))


def build_html(title, elements, style, vendor_js, diff_controls=""):
    legend_rows = []
    for kind, (shape, fill) in sorted(NODE_STYLE.items()):
        legend_rows.append(
            f'<span class=lg><span class=sw style="background:{fill}"></span>{esc(kind)}</span>')
    edge_rows = []
    for kind, (color, dash) in sorted(EDGE_STYLE.items()):
        edge_rows.append(
            f'<span class=lg><span class=ln style="background:{color};'
            f'{"border-top:2px dashed "+color+";background:transparent" if dash=="dashed" else ""}">'
            f'</span>{esc(kind)}</span>')
    ek = "".join(
        f'<label class=ek><input type=checkbox id="ek_{k}" checked> {k}</label>'
        for k in ["calls", "reads", "writes", "prompts", "invokes"])
    els_json = json.dumps(elements, separators=(",", ":"))
    style_json = json.dumps(style, separators=(",", ":"))
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>{esc(title)}</title>
<style>
  html,body{{margin:0;height:100%;font-family:Helvetica,Arial,sans-serif;color:#222}}
  #bar{{position:fixed;top:0;left:0;right:0;height:auto;background:#2c3e50;color:#fff;
        padding:8px 12px;z-index:10;display:flex;flex-wrap:wrap;gap:8px;align-items:center;
        box-shadow:0 1px 4px rgba(0,0,0,.3)}}
  #bar b{{margin-right:6px}}
  #bar button{{font-size:12px;padding:4px 9px;border:1px solid #567;border-radius:4px;
        background:#3b5168;color:#fff;cursor:pointer}}
  #bar button:hover{{background:#4a688a}}
  #search{{font-size:12px;padding:4px 8px;border-radius:4px;border:1px solid #567;width:180px}}
  .ek{{font-size:12px;margin-right:2px;cursor:pointer;user-select:none}}
  #cy{{position:absolute;top:0;left:0;right:0;bottom:0;background:#fafafa}}
  #legend{{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid #ccc;
        padding:6px 12px;font-size:11px;z-index:9;display:flex;flex-wrap:wrap;gap:10px;align-items:center}}
  .lg{{display:inline-flex;align-items:center;gap:4px;margin-right:4px}}
  .sw{{width:13px;height:13px;border:1px solid #888;display:inline-block;border-radius:3px}}
  .ln{{width:20px;height:0;border-top:3px solid #333;display:inline-block}}
  #wrap{{position:absolute;top:44px;left:0;right:0;bottom:34px}}
  #tip{{position:fixed;display:none;background:#222;color:#fff;font-size:11px;padding:3px 7px;
        border-radius:4px;pointer-events:none;z-index:20;max-width:520px;word-break:break-all}}
  .sep{{color:#89a;margin:0 2px}}
</style></head><body>
<div id=bar>
  <b>code-viz</b><span style="font-size:12px;color:#bcd">{esc(title)}</span>
  <span class=sep>|</span>
  <button id=fit>fit</button><button id=zin>zoom +</button><button id=zout>zoom -</button>
  <span class=sep>|</span>
  <button id=collapseMods>collapse all modules</button><button id=expandAll>expand all</button>
  <span class=sep>|</span>
  <input id=search placeholder="search node id...">
  <span class=sep>|</span>
  <span style="font-size:12px;color:#bcd">edges:</span>{ek}
  {diff_controls}
</div>
<div id=wrap><div id=cy></div></div>
<div id=tip></div>
<div id=legend>
  <b style="font-size:11px">nodes:</b> {''.join(legend_rows)}
  <span class=sep>|</span>
  <b style="font-size:11px">edges:</b> {''.join(edge_rows)}
  <span class=sep>|</span>
  <span style="color:#666">click node = fold downstream · click module box = fold module</span>
</div>
<script>{vendor_js}</script>
<script>
var ELEMENTS = {els_json};
var STYLE = {style_json};
{APP_JS}
</script>
</body></html>"""


def _vendor_js():
    vendor_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
    parts = []
    for v in VENDOR:
        p = os.path.join(vendor_dir, v)
        if not os.path.exists(p):
            sys.exit(f"ERROR: missing vendored lib {p} (run the vendor download)")
        with open(p) as fh:
            parts.append(f"/* ==== {v} ==== */\n" + fh.read())
    return "\n".join(parts)


def main(ir_path, out_path):
    ir = json.load(open(ir_path))
    repo = ir.get("meta", {}).get("repo", os.path.basename(out_path))
    elements = build_elements(ir["nodes"], ir["edges"])
    page = build_html(f"{repo}", elements, cy_style(), _vendor_js())
    with open(out_path, "w") as fh:
        fh.write(page)
    n_real = sum(1 for e in elements if "source" not in e["data"] and not e["data"].get("isModule"))
    n_edge = sum(1 for e in elements if "source" in e["data"])
    n_mod = sum(1 for e in elements if e["data"].get("isModule"))
    print(f"wrote {out_path} ({os.path.getsize(out_path)} bytes): "
          f"{n_real} nodes / {n_edge} edges / {n_mod} module compounds, vendored+inlined {len(VENDOR)} libs")


def main_diff(old_path, new_path, out_path):
    old, new = json.load(open(old_path)), json.load(open(new_path))
    old_r = old.get("meta", {}).get("repo", "old"); new_r = new.get("meta", {}).get("repo", "new")
    elements, (add_n, del_n, add_e, del_e) = build_elements_diff(old, new)
    controls = ('<span class=sep>|</span>'
                f'<span style="font-size:12px;color:#bcd">diff: '
                f'<b style="color:#7fe07f">+{add_n}n/+{add_e}e</b> '
                f'<b style="color:#f4a0a0">-{del_n}n/-{del_e}e</b></span>'
                '<label class=ek><input type=checkbox id="changedOnly"> changed only</label>'
                '<button id=collapseUnchanged>collapse unchanged modules</button>')
    page = build_html(f"diff {esc(old_r)} → {esc(new_r)}", elements, cy_style(), _vendor_js(), controls)
    with open(out_path, "w") as fh:
        fh.write(page)
    print(f"wrote {out_path}: diff +{add_n} nodes/+{add_e} edges, -{del_n} nodes/-{del_e} edges "
          f"(green=added, red=removed, grey=unchanged)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 3 and a[0] == "--diff":
        sys.exit("usage: python viz/cyto.py --diff <old.ir.json> <new.ir.json> <out.html>")
    if len(a) == 4 and a[0] == "--diff":
        main_diff(a[1], a[2], a[3])
    elif len(a) == 2:
        main(a[0], a[1])
    else:
        sys.exit("usage: python viz/cyto.py <ir.json> <out.html>\n"
                 "       python viz/cyto.py --diff <old.ir.json> <new.ir.json> <out.html>")
