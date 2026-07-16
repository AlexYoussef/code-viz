#!/usr/bin/env bash
# Source tree -> canonical IR json.  Runs the full extractor pipeline:
#   pysrc2cpg (Joern CPG) -> export.sc (call edges) -> emit_ir.py (calls + db reads/writes -> IR)
# Usage: build_ir.sh <src_dir> <out_ir.json>
set -euo pipefail
SRC="$1"; OUT="$2"
SELF="$0"; while [ -L "$SELF" ]; do t="$(readlink "$SELF")"; case "$t" in /*) SELF="$t";; *) SELF="$(dirname "$SELF")/$t";; esac; done
CV="$(cd "$(dirname "$SELF")/..")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

/opt/homebrew/bin/pysrc2cpg "$SRC" --output "$TMP/cpg.bin" >/dev/null 2>&1
/opt/homebrew/bin/joern --script "$CV/extract/export.sc" \
  --param cpgFile="$TMP/cpg.bin" --param out="$TMP/joern.json" >/dev/null 2>&1
( cd "$CV" && uv run --quiet --with sqlglot python extract/emit_ir.py "$SRC" "$TMP/joern.json" "$OUT" >/dev/null )
echo "built IR: $OUT"
