"""Visual diff of two IR snapshots -> colored union graph (added=green, removed=red, same=grey)
in a self-contained pan/zoom HTML, plus a structured changelog.

Snapshot = a committed ir.json (canonical/deterministic). Diff = id-set difference of nodes+edges.
Usage: python diff_ir.py <old.ir.json> <new.ir.json> <out_prefix>
       -> <out_prefix>.diff.dot, <out_prefix>.diff.html (pan/zoom), prints changelog
"""
import json, sys, html, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ir_to_dot import render_svg, panzoom_page  # shared pan/zoom shell

NODE_SHAPE = {"function":"box","method":"box","module_entry":"Mdiamond","class":"box",
              "sql_call":"ellipse","http_call":"hexagon","http_endpoint":"hexagon",
              "table":"cylinder","view":"cylinder"}
ADD_FILL, DEL_FILL, SAME_FILL = "#b7f0b7", "#f4b7b7", "#eeeeee"
ADD_LINE, DEL_LINE, SAME_LINE = "#1a7f1a", "#c0392b", "#999999"

def esc(s): return html.escape(str(s)).replace('"','\\"')
def short(nid):
    if nid.startswith("db::"): return nid[4:]
    if "::" in nid:
        f,q=nid.split("::",1); return f"{f.split('/')[-1]}\\n{q}"
    return nid

def build_dot(old, new):
    on = {n["id"]: n for n in old["nodes"]}; nn = {n["id"]: n for n in new["nodes"]}
    oe = {(e["src"],e["dst"],e["kind"]) for e in old["edges"]}
    ne = {(e["src"],e["dst"],e["kind"]) for e in new["edges"]}

    def nstat(i): return "add" if i in nn and i not in on else "del" if i in on and i not in nn else "same"
    def estat(k): return "add" if k in ne and k not in oe else "del" if k in oe and k not in ne else "same"
    allnodes = sorted(set(on)|set(nn)); alledges = sorted(oe|ne)
    add_n=sum(nstat(i)=="add" for i in allnodes); del_n=sum(nstat(i)=="del" for i in allnodes)
    add_e=sum(estat(k)=="add" for k in alledges); del_e=sum(estat(k)=="del" for k in alledges)
    changelog = {
        "added_nodes":   sorted(i for i in allnodes if nstat(i)=="add"),
        "removed_nodes": sorted(i for i in allnodes if nstat(i)=="del"),
        "added_edges":   sorted(f"{s} -{k}-> {d}" for (s,d,k) in alledges if estat((s,d,k))=="add"),
        "removed_edges": sorted(f"{s} -{k}-> {d}" for (s,d,k) in alledges if estat((s,d,k))=="del"),
    }

    L=["digraph diff {","  rankdir=LR;",
       '  graph [fontname="Helvetica", nodesep=0.3, ranksep=0.7, splines=true];',
       '  node  [fontname="Helvetica", fontsize=10, style="filled,rounded"];',
       '  edge  [fontname="Helvetica", fontsize=8];',
       f'  labelloc=t; fontsize=15; label="IR diff:  +{add_n} nodes / +{add_e} edges (green)   '
       f'-{del_n} nodes / -{del_e} edges (red)";']
    for i in allnodes:
        st=nstat(i); kind=(nn.get(i) or on.get(i))["kind"]; shape=NODE_SHAPE.get(kind,"box")
        fill={"add":ADD_FILL,"del":DEL_FILL,"same":SAME_FILL}[st]
        line={"add":ADD_LINE,"del":DEL_LINE,"same":SAME_LINE}[st]
        extra = ', style="filled,rounded,dashed"' if st=="del" else ''
        L.append(f'  "{esc(i)}" [label="{esc(short(i))}", shape={shape}, fillcolor="{fill}", '
                 f'color="{line}", penwidth={2 if st!="same" else 1}{extra}];')
    for k in alledges:
        s,d,kind=k; st=estat(k)
        color={"add":ADD_LINE,"del":DEL_LINE,"same":SAME_LINE}[st]
        style="dashed" if st=="del" else "solid"
        pw={"add":2.0,"del":2.0,"same":0.8}[st]
        L.append(f'  "{esc(s)}" -> "{esc(d)}" [color="{color}", style={style}, penwidth={pw}];')
    L.append('  subgraph cluster_lg { label="diff legend"; style=dashed; color="#999";')
    L.append(f'    a [label="added", shape=box, fillcolor="{ADD_FILL}", color="{ADD_LINE}", penwidth=2, style="filled,rounded"];')
    L.append(f'    d [label="removed", shape=box, fillcolor="{DEL_FILL}", color="{DEL_LINE}", penwidth=2, style="filled,rounded,dashed"];')
    L.append(f'    s [label="unchanged", shape=box, fillcolor="{SAME_FILL}", color="{SAME_LINE}", style="filled,rounded"];')
    L.append('  }')
    L.append("}")
    return "\n".join(L)+"\n", (add_n,del_n,add_e,del_e), changelog

def main(old_p, new_p, out_prefix):
    old, new = json.load(open(old_p)), json.load(open(new_p))
    old_lbl = old.get("meta",{}).get("repo","old"); new_lbl = new.get("meta",{}).get("repo","new")
    dot, (add_n,del_n,add_e,del_e), cl = build_dot(old, new)
    open(f"{out_prefix}.diff.dot","w").write(dot)
    hint = (f'<b style="color:#1a7f1a">+{add_n} nodes / +{add_e} edges</b> &nbsp; '
            f'<b style="color:#c0392b">−{del_n} nodes / −{del_e} edges</b> &nbsp; green=added, red(dashed)=removed, grey=unchanged.')
    page = panzoom_page(f"code-viz diff — {esc(old_lbl)} → {esc(new_lbl)}",
                        [{"id":"diff","label":"IR diff","height":"86vh","hint":hint,"svg":render_svg(dot)}])
    open(f"{out_prefix}.diff.html","w").write(page)
    # structured changelog to stdout + file
    json.dump(cl, open(f"{out_prefix}.diff.changelog.json","w"), indent=1)
    print(f"diff: +{add_n}/-{del_n} nodes, +{add_e}/-{del_e} edges -> {out_prefix}.diff.html")
    for i in cl["added_nodes"]:   print("  + node ", i)
    for i in cl["removed_nodes"]: print("  - node ", i)
    for e in cl["added_edges"]:   print("  + edge ", e)
    for e in cl["removed_edges"]: print("  - edge ", e)

if __name__=="__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3])
