#!/usr/bin/env bash
# Git-driven snapshot + visual diff of a repo's code graph.
#   snapshot <repo> <ref> <out_ir.json>     — build the IR for a git ref
#   diff     <repo> <refA> <refB> [outpfx]   — build both, render zoomable colored diff + changelog
# Snapshots are just committed ir.json files (canonical/deterministic), so any two are diffable.
set -euo pipefail
SELF="$0"; while [ -L "$SELF" ]; do t="$(readlink "$SELF")"; case "$t" in /*) SELF="$t";; *) SELF="$(dirname "$SELF")/$t";; esac; done
CV="$(cd "$(dirname "$SELF")/.." && pwd)"

_snapshot(){ # repo ref out_ir
  local repo="$1" ref="$2" out="$3" tmp
  tmp="$(mktemp -d)"
  git -C "$repo" archive "$ref" | tar -x -C "$tmp"          # source tree at that ref
  bash "$CV/viz/build_ir.sh" "$tmp" "$out"
  rm -rf "$tmp"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  snapshot) _snapshot "$1" "$2" "$3" ;;
  diff)
    repo="$1"; a="$2"; b="$3"; out="${4:-$CV/viz/gitdiff}"
    ta="$(mktemp).ir.json"; tb="$(mktemp).ir.json"
    echo "snapshot $a ..."; _snapshot "$repo" "$a" "$ta"
    echo "snapshot $b ..."; _snapshot "$repo" "$b" "$tb"
    # interactive Cytoscape diff (primary) + text changelog (from diff_ir)
    ( cd "$CV" && uv run --quiet --with sqlglot python viz/cyto.py --diff "$ta" "$tb" "$out.cytodiff.html" )
    ( cd "$CV" && uv run --quiet --with sqlglot python viz/diff_ir.py "$ta" "$tb" "$out" >/dev/null 2>&1 ) || true
    echo "interactive diff -> $out.cytodiff.html   (changelog: $out.diff.changelog.json)"
    command -v open >/dev/null && open "$out.cytodiff.html" || true
    ;;
  *) echo "usage: codeviz-git.sh snapshot <repo> <ref> <out_ir.json>"
     echo "       codeviz-git.sh diff <repo> <refA> <refB> [out_prefix]"; exit 1 ;;
esac
