"""Generalized, config-free extractor. No repo-specific constants.
  py-SQL: base DB-API/SQLAlchemy sinks + TRANSITIVE wrapper-sink discovery (fixpoint)
          + module-level constant folding + intra-fn reaching-defs + param-awareness.
  calls : Joern CPG call graph (joern_raw.json) + annotation->class-registry method resolver
          (import-aware). Then scored vs the ground-truth oracle.
Usage: codeviz.py <repo_path> <joern_raw.json> <oracle.json>
"""
import ast, re, json, glob, os, sys
import sqlglot
from sqlglot import exp

REPO, JOERN, ORACLE = sys.argv[1], sys.argv[2], sys.argv[3]

# ---- base SQL sinks (universal DB-API / SQLAlchemy primitives), ANY-arg scan ----
BASE_SINKS = {"execute","executemany","executescript","exec_driver_sql","stream"}

def rel_of(p): return os.path.relpath(p, REPO)
def is_py(p): return p.endswith(".py") and not rel_of(p).startswith("tests")
FILES = {rel_of(p): ast.parse(open(p).read()) for p in glob.glob(f"{REPO}/**/*.py", recursive=True) if is_py(p)}

# ---- parent maps + qualname ----
PARENT = {}
for tree in FILES.values():
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n): PARENT[c] = n
def qual(node):
    parts=[]; cur=node
    while cur is not None:
        if isinstance(cur,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): parts.append(cur.name)
        cur=PARENT.get(cur)
    return ".".join(reversed(parts))

# ---- signatures: funcname -> [param names] (first def wins; union on collision) ----
SIG={}
for tree in FILES.values():
    for n in ast.walk(tree):
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            SIG.setdefault(n.name, [a.arg for a in list(n.args.args)+list(n.args.kwonlyargs)])

# ---- module-level string constants per file (general SCHEMA-style folding) ----
def const_str_value(v):
    """literal str, or os.getenv('X','default')/os.environ.get('X','default') -> the default string."""
    if isinstance(v, ast.Constant) and isinstance(v.value,str): return v.value
    if isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute):
        attr=v.func.attr
        if attr in ("getenv","get") and len(v.args)>=2 and isinstance(v.args[1],ast.Constant) \
           and isinstance(v.args[1].value,str):
            return v.args[1].value
    return None

MODCONST={}
for rel,tree in FILES.items():
    d={}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets)==1 and isinstance(stmt.targets[0],ast.Name):
            sv=const_str_value(stmt.value)
            if sv is not None: d[stmt.targets[0].id]=sv
    MODCONST[rel]=d

# follow imports: a file's usable constants = its own + string constants it imports (general SCHEMA case)
def resolve_module(rel_file, node):
    if node.level and node.level>0:
        base=os.path.dirname(rel_file)
        for _ in range(node.level-1): base=os.path.dirname(base)
        path=os.path.join(base, *(node.module.split("."))) if node.module else base
    elif node.module:
        path=os.path.join(*node.module.split("."))
    else:
        return None
    cand=path+".py"
    return cand if cand in FILES else None

FILECONST={}
for rel,tree in FILES.items():
    d=dict(MODCONST[rel])
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            src=resolve_module(rel, n)
            if src:
                for a in n.names:
                    if a.name in MODCONST.get(src,{}):
                        d[a.asname or a.name]=MODCONST[src][a.name]
    FILECONST[rel]=d

def unwrap_text(a):
    if isinstance(a,ast.Call) and isinstance(a.func,ast.Attribute) and a.func.attr=="text":
        return a.args[0] if a.args else None
    if isinstance(a,ast.Call) and isinstance(a.func,ast.Name) and a.func.id=="text":
        return a.args[0] if a.args else None
    return a

def resolve(node, env, consts):
    if node is None: return None
    if isinstance(node, ast.Constant) and isinstance(node.value,str): return node.value
    if isinstance(node, ast.JoinedStr):
        out=[]
        for v in node.values:
            if isinstance(v, ast.Constant): out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                nm=v.value.id if isinstance(v.value,ast.Name) else None
                out.append(consts.get(nm, env.get(nm, "x")) if nm else "x")
            else: out.append("x")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l=resolve(node.left,env,consts); r=resolve(node.right,env,consts)
        return (l or "")+(r or "") if (l or r) else None
    if isinstance(node, ast.Name):
        return env.get(node.id, consts.get(node.id))
    return None

# ---- sink args at a call site: returns list of candidate SQL arg nodes ----
def sink_sql_args(call, sink_params):
    """call: ast.Call; sink_params: methodname -> set(param-names) or {'*ANY*'}."""
    m = call.func.attr if isinstance(call.func, ast.Attribute) else (call.func.id if isinstance(call.func,ast.Name) else None)
    if m not in sink_params: return []
    want = sink_params[m]
    cands=[]
    if "*ANY*" in want:
        cands=[unwrap_text(a) for a in call.args]
    else:
        params = SIG.get(m, [])
        # bound-method call: receiver.method(...) omits self/cls from the arg list
        if params and params[0] in ("self","cls") and isinstance(call.func, ast.Attribute):
            params = params[1:]
        # positional
        for i,a in enumerate(call.args):
            if i < len(params) and params[i] in want: cands.append(unwrap_text(a))
        # keyword
        for kw in call.keywords:
            if kw.arg in want: cands.append(unwrap_text(kw.value))
    return [c for c in cands if c is not None]

# ---- TRANSITIVE sink discovery (fixpoint): a fn that forwards a param into a sink IS a sink ----
sink_params = {b:{"*ANY*"} for b in BASE_SINKS}
changed=True
while changed:
    changed=False
    for rel,tree in FILES.items():
        for fn in [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
            pnames={a.arg for a in list(fn.args.args)+list(fn.args.kwonlyargs)}
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
                for cand in sink_sql_args(call, sink_params):
                    if isinstance(cand, ast.Name) and cand.id in pnames:
                        cur=sink_params.setdefault(fn.name,set())
                        if cand.id not in cur: cur.add(cand.id); changed=True

# ---- reaching-defs env within a function ----
def build_env(fn, consts):
    env={}
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign) and len(stmt.targets)==1 and isinstance(stmt.targets[0],ast.Name):
            v=resolve(stmt.value, env, consts)
            if v is not None: env[stmt.targets[0].id]=v
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target,ast.Name) and isinstance(stmt.op,ast.Add):
            add=resolve(stmt.value, env, consts)
            if add is not None: env[stmt.target.id]=env.get(stmt.target.id,"")+add
    return env

def looks_sql(s): return bool(s) and re.search(r"\b(select|insert\s+into|update|delete\s+from|with)\b",s,re.I)
def clean(s):
    s=re.sub(r"%\(\w+\)s","null",s); s=s.replace("%s","null")
    s=re.sub(r"(?<!:):\w+","null",s)   # named params, but keep Postgres ::type casts
    return s
def tables_and_op(s):
    try: t=sqlglot.parse_one(clean(s), read="postgres")
    except Exception: return None,set()
    op="insert" if isinstance(t,exp.Insert) else "update" if isinstance(t,exp.Update) else "delete" if isinstance(t,exp.Delete) else "select"
    tbls={(f"{x.db}.{x.name}" if x.db else x.name) for x in t.find_all(exp.Table)}
    return ("write" if op in("insert","update","delete") else "read"), tbls

# ---- extraction: db reads/writes edges ----
db_edges=set(); unbound=set()
for rel,tree in FILES.items():
    consts=FILECONST[rel]
    for fn in [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
        pnames={a.arg for a in list(fn.args.args)+list(fn.args.kwonlyargs)}
        env=build_env(fn, consts)
        owner=f"{rel}::{qual(fn)}"
        for call in [n for n in ast.walk(fn) if isinstance(n,ast.Call)]:
            for cand in sink_sql_args(call, sink_params):
                if isinstance(cand, ast.Name) and cand.id in pnames and cand.id not in env:
                    unbound.add(owner); continue
                s=resolve(cand, env, consts)
                if looks_sql(s):
                    op,tbls=tables_and_op(s)
                    for t in tbls: db_edges.add((owner,op,t))

# ---- ORM layer: Model->__tablename__ registry + select()/session.add/get -> reads/writes ----
MODEL_TABLE={}
for rel,tree in FILES.items():
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            for s in n.body:
                if isinstance(s, ast.Assign) and any(isinstance(t,ast.Name) and t.id=="__tablename__" for t in s.targets) \
                   and isinstance(s.value, ast.Constant) and isinstance(s.value.value,str):
                    MODEL_TABLE[n.name]=s.value.value

def enclosing_fn(node):
    cur=PARENT.get(node)
    while cur is not None and not isinstance(cur,(ast.FunctionDef,ast.AsyncFunctionDef)): cur=PARENT.get(cur)
    return cur

def model_of_expr(node, menv):
    """expr referring to a Model: Model / Model.col / Model(...) / a var typed as Model -> table name."""
    if isinstance(node, ast.Name):
        if node.id in MODEL_TABLE: return MODEL_TABLE[node.id]
        if node.id in menv: return menv[node.id]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in MODEL_TABLE:
        return MODEL_TABLE[node.value.id]
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in MODEL_TABLE:  # Model(...)
            return MODEL_TABLE[node.func.id]
    return None

def model_in_select(node, menv):
    """select(Model[, Model.col...]) -> table of first model arg."""
    if isinstance(node, ast.Call) and ((isinstance(node.func,ast.Name) and node.func.id=="select")
                                       or (isinstance(node.func,ast.Attribute) and node.func.attr=="select")):
        for a in node.args:
            t=model_of_expr(a, menv)
            if t: return t
    return None

def model_of_value(v, menv):
    """infer a Model table for an assigned value: Model(...)/session.get(Model)/session.scalar(select(Model))/x or Model()."""
    if isinstance(v, ast.Call):
        if isinstance(v.func, ast.Name) and v.func.id in MODEL_TABLE: return MODEL_TABLE[v.func.id]
        if isinstance(v.func, ast.Attribute):
            if v.func.attr=="get" and v.args:               # session.get(Model, pk)
                return model_of_expr(v.args[0], menv)
            if v.func.attr in ("scalar","execute") and v.args:  # session.scalar(select(Model)...)
                return model_in_select(v.args[0], menv)
    if isinstance(v, ast.BoolOp):                            # existing or Model(...)
        for x in v.values:
            t=model_of_value(x, menv)
            if t: return t
    if isinstance(v, ast.Name):
        return menv.get(v.id)
    return None

for rel,tree in FILES.items():
    for fn in [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
        owner=f"{rel}::{qual(fn)}"
        # local Model-typed var env
        menv={}
        for stmt in ast.walk(fn):
            if isinstance(stmt, ast.Assign) and len(stmt.targets)==1 and isinstance(stmt.targets[0],ast.Name):
                t=model_of_value(stmt.value, menv)
                if t: menv[stmt.targets[0].id]=t
        for call in [n for n in ast.walk(fn) if isinstance(n,ast.Call)]:
            f=call.func
            # reads: select(Model...) anywhere
            t=model_in_select(call, menv)
            if t: db_edges.add((owner,"read",t))
            if isinstance(f, ast.Attribute):
                if f.attr in ("get","query") and call.args:   # session.get/query(Model,...)
                    tm=model_of_expr(call.args[0], menv)
                    if tm: db_edges.add((owner,"read",tm))
                elif f.attr=="add" and call.args:              # session.add(instance) -> write
                    tm=model_of_expr(call.args[0], menv) or model_of_value(call.args[0], menv)
                    if tm: db_edges.add((owner,"write",tm))
                elif f.attr=="merge" and call.args:
                    tm=model_of_expr(call.args[0], menv) or model_of_value(call.args[0], menv)
                    if tm: db_edges.add((owner,"write",tm))
                elif f.attr=="delete" and call.args:
                    tm=model_of_expr(call.args[0], menv) or model_of_value(call.args[0], menv)
                    if tm: db_edges.add((owner,"delete",tm))

# ---- annotation -> class-registry method resolver (import-aware) ----
# registry: classname -> set(files defining it)
CLASSREG={}
for rel,tree in FILES.items():
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            CLASSREG.setdefault(n.name,{})[rel]={m.name for m in n.body if isinstance(m,(ast.FunctionDef,ast.AsyncFunctionDef))}
# per-file import map: localname -> module (for `from mod import Class` / `import mod`)
def import_map(tree):
    im={}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names: im[a.asname or a.name]=n.module
        elif isinstance(n, ast.Import):
            for a in n.names: im[(a.asname or a.name).split('.')[0]]=a.name
    return im

def ann_name(node):
    if isinstance(node, ast.Name): return node.id, None
    if isinstance(node, ast.Attribute): return node.attr, (node.value.id if isinstance(node.value,ast.Name) else None)
    return None,None

call_edges=set()
for rel,tree in FILES.items():
    im=import_map(tree)
    for fn in [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
        pmap={}
        for a in list(fn.args.args)+list(fn.args.kwonlyargs):
            if a.annotation is not None:
                cname,modq=ann_name(a.annotation)
                if cname in CLASSREG:
                    defs=CLASSREG[cname]
                    # disambiguate same-named classes via the module qualifier's import target
                    if len(defs)>1 and modq and modq in im:
                        target=[f for f in defs if im[modq].replace('.','/') in f]
                        deffile=target[0] if target else sorted(defs)[0]
                    else:
                        deffile=sorted(defs)[0]
                    pmap[a.arg]=(cname,deffile)
        if not pmap: continue
        owner=f"{rel}::{qual(fn)}"
        for call in [n for n in ast.walk(fn) if isinstance(n,ast.Call)]:
            f=call.func
            if isinstance(f,ast.Attribute) and isinstance(f.value,ast.Name) and f.value.id in pmap:
                cls,deffile=pmap[f.value.id]
                if f.attr in CLASSREG[cls][deffile]:
                    call_edges.add((owner, f"{deffile}::{cls}.{f.attr}"))

# ---- fix 2+3: instance-dispatch + import-aware name/module call resolution ----
DEF=set()
for rel,tree in FILES.items():
    for fn in [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
        DEF.add(f"{rel}::{qual(fn)}")

def modpath_to_file(dotted, level, rel):
    """dotted module path (+ relative level) -> repo .py file, trying '' and 'src/' roots and package __init__."""
    if level and level>0:
        base=os.path.dirname(rel)
        for _ in range(level-1): base=os.path.dirname(base)
        parts=dotted.split('.') if dotted else []
        p=os.path.join(base,*parts) if parts else base
        cands=[p+'.py', os.path.join(p,'__init__.py')]
    else:
        parts=dotted.split('.')
        cands=[]
        for pre in ('','src'):
            b=os.path.join(pre,*parts) if pre else os.path.join(*parts)
            cands+=[b+'.py', os.path.join(b,'__init__.py')]
    for c in cands:
        if c in FILES: return c
    return None

IMPORT_SYM={}; IMPORT_MOD={}; INSTANCE={}
for rel,tree in FILES.items():
    syms={}; mods={}; inst={}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            modfile=modpath_to_file(n.module or "", n.level, rel)
            for a in n.names:
                sub=modpath_to_file(((n.module+"."+a.name) if n.module else a.name), n.level, rel)
                local=a.asname or a.name
                if sub and sub!=modfile: mods[local]=sub          # imported a submodule (module alias)
                elif modfile: syms[local]=modfile                  # imported a symbol from a module
        elif isinstance(n, ast.Import):
            for a in n.names:
                mf=modpath_to_file(a.name, 0, rel)
                if mf: mods[(a.asname or a.name).split('.')[0]]=mf
        elif isinstance(n, ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) \
             and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id in CLASSREG:
            inst[n.targets[0].id]=n.value.func.id                  # name = Class()  (module-level instance)
    IMPORT_SYM[rel]=syms; IMPORT_MOD[rel]=mods; INSTANCE[rel]=inst

def class_dispatch_target(cls):
    """instance(...)  ->  Class.forward (Module convention) else Class.__call__."""
    for f,meths in CLASSREG.get(cls,{}).items():
        if "forward" in meths: return f"{f}::{cls}.forward"
        if "__call__" in meths: return f"{f}::{cls}.__call__"
    return None

resolved_edges=set()
for rel,tree in FILES.items():
    syms=IMPORT_SYM[rel]; mods=IMPORT_MOD[rel]; inst=INSTANCE[rel]
    for fn in [n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))]:
        owner=f"{rel}::{qual(fn)}"
        for call in [n for n in ast.walk(fn) if isinstance(n,ast.Call)]:
            f=call.func
            if isinstance(f, ast.Name):
                nm=f.id
                if nm in inst:                                     # fix 2: instance dispatch
                    tgt=class_dispatch_target(inst[nm])
                    if tgt and tgt in DEF: resolved_edges.add((owner,tgt))
                elif nm in syms:                                   # fix 3: from X import foo; foo()
                    tgt=f"{syms[nm]}::{nm}"
                    if tgt in DEF: resolved_edges.add((owner,tgt))
                else:                                              # same-file function
                    tgt=f"{rel}::{nm}"
                    if tgt in DEF: resolved_edges.add((owner,tgt))
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in mods:
                tgt=f"{mods[f.value.id]}::{f.attr}"                # fix 3: module.attr()
                if tgt in DEF: resolved_edges.add((owner,tgt))
call_edges = call_edges | resolved_edges

# ---- merge annotation edges with Joern call graph, score vs oracle ----
joern=json.load(open(JOERN)); oracle=json.load(open(ORACLE))
RF=set(oracle["modules"])
def norm(fn):
    if ":<module>." in fn:
        a,b=fn.split(":<module>.",1); return f"{a}::{b}"
    return None
oracle_fn={n["id"] for n in oracle["nodes"] if n["kind"] in {"function","method","module_entry"}}
oracle_calls={(e["src"],e["dst"]) for e in oracle["edges"] if e["kind"]=="calls"}
joern_calls={(norm(c["caller"]),norm(c["callee"])) for c in joern["calls"]
             if norm(c["caller"]) and norm(c["callee"]) and c["file"] in RF}
merged=joern_calls | call_edges
merged={(s,d) for (s,d) in merged if not (s==d and s.endswith(".__init__"))}  # fix 1: drop super().__init__ self-loop FPs
modeled={(s,d) for (s,d) in merged if s in oracle_fn and d in oracle_fn}
chit=oracle_calls & modeled
print("=== GENERALIZED (no repo-specific config) ===")
print(f"discovered wrapper sinks (beyond base): {sorted(k for k in sink_params if k not in BASE_SINKS)}")
print(f"annotation-recovered call edges: {len(call_edges)}")
print(f"CALL GRAPH: recall {len(chit)}/{len(oracle_calls)} = {len(chit)/len(oracle_calls):.2f} | precision {len(chit)}/{len(modeled)} = {len(chit)/len(modeled):.2f}")

owner_of={e["dst"]:e["src"] for e in oracle["edges"] if e["kind"]=="invokes"}
orc_db=set()
for e in oracle["edges"]:
    if e["kind"] in ("reads","writes"):
        o=owner_of.get(e["src"])
        if o: orc_db.add((o,"write" if e["kind"]=="writes" else "read", e["dst"].replace("db::","")))
# scope produced db edges to functions the oracle actually models (fair for scoped oracles)
prod_db={(o,op,t) for (o,op,t) in db_edges if o in oracle_fn}
dhit=prod_db & orc_db
print(f"DB-ACCESS: recall {len(dhit)}/{len(orc_db)} = {len(dhit)/len(orc_db):.2f} | precision {len(dhit)}/{len(prod_db)} = {len(dhit)/len(prod_db) if prod_db else 0:.2f}")
print(f"unbound (dynamic user-SQL, correctly no table edge): {len(unbound)} fns")
dmiss=orc_db-prod_db; dfp=prod_db-orc_db
if dmiss: print("  DB MISSED:", sorted(dmiss))
if dfp: print("  DB FALSE-POS:", sorted(dfp))
