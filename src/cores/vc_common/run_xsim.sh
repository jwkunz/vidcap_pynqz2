#!/usr/bin/env bash
#
# run_xsim.sh - Compile and run a Verilog/SystemVerilog testbench with Vivado xsim.
#
# Usage: run_xsim.sh <top_module> <source files...>
# Output goes to workspace/vivado/sim/<top_module>/ (untracked). Exit status is
# non-zero if the simulation prints "TEST FAILED" or never prints "TEST PASSED".
#
set -euo pipefail
TOP="$1"; shift
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
OUT="$ROOT/workspace/vivado/sim/$TOP"
if [ -z "${XILINX_VIVADO:-}" ]; then set +eu; source "${AMD_ROOT:-$HOME/AMD}/2025.2/Vitis/settings64.sh" >/dev/null 2>&1; set -eu; fi

FILES=(); for f in "$@"; do FILES+=("$(realpath "$f")"); done
rm -rf "$OUT"; mkdir -p "$OUT"; cd "$OUT"
xvlog -sv "${FILES[@]}" > xvlog.log 2>&1 || { cat xvlog.log; exit 1; }
xelab -debug off -s sim "$TOP" > xelab.log 2>&1 || { cat xelab.log; exit 1; }
xsim sim -runall > xsim.log 2>&1 || true
grep -vE '^(INFO|Time resolution|\*\*\*|Vivado|Copyright|#)' xsim.log | sed '/^$/d' | tail -n "${TAIL:-40}"
grep -q "TEST PASSED" xsim.log && ! grep -q "TEST FAILED" xsim.log
