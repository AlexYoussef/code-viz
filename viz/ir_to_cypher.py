"""Flow/DAG visualization: IR JSON -> Neo4j Cypher loader + example query pack.

Neo4j is the query-first exploration index (D2): load the IR, then traverse with Cypher
(blast-radius, call chains, table writers). Emits two files:
  <out>.load.cypher     — idempotent MERGE of nodes + typed relationships
  <out>.queries.cypher  — ready-to-run investigation queries

Run:  cypher-shell -u neo4j -p <pw> -f <out>.load.cypher
      (spin up Neo4j: docker run -p7474:7474 -p7687:7687 -e NEO4J_AUTH=neo4j/test neo4j:5)
Usage: python ir_to_cypher.py <ir.json> <out_prefix>
"""
import json, sys

# node.kind -> Neo4j label
LABEL = {
    "function": "Function", "method": "Method", "module_entry": "Entry", "class": "Class",
    "sql_call": "SqlCall", "http_call": "HttpCall", "http_endpoint": "HttpEndpoint",
    "table": "Table", "view": "View",
}
# edge.kind -> relationship type
REL = {"calls": "CALLS", "invokes": "INVOKES", "reads": "READS",
       "writes": "WRITES", "derives": "DERIVES", "http": "HTTP", "maps": "MAPS"}

def cq(s): return str(s).replace("\\", "\\\\").replace("'", "\\'")

def main(ir_path, out_prefix):
    ir = json.load(open(ir_path))
    nodes = sorted(ir["nodes"], key=lambda n: n["id"])
    edges = sorted(ir["edges"], key=lambda e: (e["src"], e["dst"], e["kind"]))

    L = ["// --- constraints (id unique per label) ---"]
    for lbl in sorted(set(LABEL.values())):
        L.append(f"CREATE CONSTRAINT {lbl.lower()}_id IF NOT EXISTS FOR (n:{lbl}) REQUIRE n.id IS UNIQUE;")
    L.append("\n// --- nodes ---")
    for n in nodes:
        lbl = LABEL.get(n["kind"], "Node")
        name = n["id"].split("::")[-1] if "::" in n["id"] else n["id"].replace("db::", "")
        props = f"id:'{cq(n['id'])}', name:'{cq(name)}', kind:'{n['kind']}'"
        if n.get("file"): props += f", file:'{cq(n['file'])}'"
        L.append(f"MERGE (n:{lbl} {{id:'{cq(n['id'])}'}}) SET n += {{{props}}};")
    L.append("\n// --- relationships ---")
    for e in edges:
        rt = REL.get(e["kind"], "REL")
        L.append(f"MATCH (a {{id:'{cq(e['src'])}'}}),(b {{id:'{cq(e['dst'])}'}}) "
                 f"MERGE (a)-[:{rt}]->(b);")
    open(f"{out_prefix}.load.cypher", "w").write("\n".join(L) + "\n")

    Q = f"""// ===== code-viz investigation queries ({ir['meta'].get('repo','repo')}) =====

// 1. BLAST RADIUS of a table — every function that transitively reaches a write/read to it.
//    (change 'signals' to any table; ::text match keeps it schema-agnostic)
MATCH (t:Table) WHERE t.name CONTAINS 'signals'
MATCH p=(f:Function)-[:CALLS*0..]->()-[:INVOKES]->(:SqlCall)-[:WRITES|READS]->(t)
RETURN DISTINCT f.id AS writer_or_reader, t.name AS table ORDER BY writer_or_reader;

// 2. CALL CHAIN from an entrypoint — what does run_scan reach?
MATCH p=(e {{name:'run_scan'}})-[:CALLS*1..6]->(f)
RETURN [n IN nodes(p) | n.name] AS chain, length(p) AS depth ORDER BY depth LIMIT 40;

// 3. TABLE WRITERS — which functions write which tables (the persistence map).
MATCH (f)-[:INVOKES]->(s:SqlCall)-[:WRITES]->(t:Table)
RETURN f.name AS fn, t.name AS writes_table ORDER BY t.name, fn;

// 4. WHAT DOES A FUNCTION TOUCH — all tables reachable from a function's call subtree.
MATCH (f {{name:'run_scan'}})-[:CALLS*0..]->()-[:INVOKES]->(:SqlCall)-[r:READS|WRITES]->(t:Table)
RETURN DISTINCT type(r) AS op, t.name AS table ORDER BY op, table;

// 5. UNREACHED (dead-ish) functions — no incoming CALLS (candidate entrypoints or dead code).
MATCH (f:Function) WHERE NOT ()-[:CALLS]->(f)
RETURN f.id ORDER BY f.id;

// 6. FAN-IN hotspots — most-called functions (refactor risk).
MATCH (f)<-[:CALLS]-() RETURN f.name, count(*) AS callers ORDER BY callers DESC LIMIT 15;
"""
    open(f"{out_prefix}.queries.cypher", "w").write(Q)
    print(f"wrote {out_prefix}.load.cypher ({len(nodes)} nodes, {len(edges)} rels) + {out_prefix}.queries.cypher")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
