#!/usr/bin/env bash
#
# run_hls.sh - Run a Vitis HLS step for one core.
#
# Usage: run_hls.sh <core> <step>
#   step = csim     C simulation against the testbench           (small frame)
#          cosim    C synthesis + RTL co-simulation               (small frame)
#          syn      C synthesis at full 1280x720                   (reports)
#          package  C synthesis + export as a Vivado IP           (full size)
#
# The core's config template is src/cores/<core>/hls.cfg.in. @TOKENS@ are filled in here and
# the result is written under workspace/vitishls/ (untracked). Small-frame builds use
# SIM_W x SIM_H so simulation finishes in seconds.
#
set -euo pipefail
CORE="$1"; STEP="$2"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SRC="$ROOT/src/cores/$CORE"
COMMON="$ROOT/src/cores/vc_common"
[ -f "$SRC/hls.cfg.in" ] || { echo "no $SRC/hls.cfg.in" >&2; exit 2; }
if [ -z "${XILINX_VITIS:-}" ]; then set +eu; source "${AMD_ROOT:-$HOME/AMD}/2025.2/Vitis/settings64.sh" >/dev/null 2>&1; set -eu; fi

# small frame for simulation; a core can override it in src/cores/<core>/sim_dims ("W H")
SIM_W=64; SIM_H=48
[ -f "$SRC/sim_dims" ] && read -r SIM_W SIM_H < "$SRC/sim_dims"
SIM_W="${SIM_W_OVERRIDE:-$SIM_W}"; SIM_H="${SIM_H_OVERRIDE:-$SIM_H}"
case "$STEP" in
  csim|cosim) W=$SIM_W; H=$SIM_H; CTRL=""; WS="$ROOT/workspace/vitishls/${CORE}_sim" ;;
  syn|package) W=1280; H=720; CTRL="-DVC_CTRL_NONE"; WS="$ROOT/workspace/vitishls/${CORE}_build" ;;
  *) echo "unknown step '$STEP'" >&2; exit 2 ;;
esac
mkdir -p "$WS"
CFG="$WS/hls.cfg"
sed -e "s#@SRC@#$SRC#g" -e "s#@COMMON@#$COMMON#g" -e "s#@W@#$W#g" -e "s#@H@#$H#g" -e "s#@CTRL@#$CTRL#g" -e "s#@SNAP_DEPTH@#$((W * 3 / 8 * H))#g" "$SRC/hls.cfg.in" > "$CFG"

case "$STEP" in
  csim)    FLAG="--csim" ;;
  cosim)   FLAG="--cosim" ;;
  syn)     FLAG="--syn" ;;
  package) FLAG="--package" ;;
esac
cd "$WS"
if [ "$STEP" = "cosim" ] || [ "$STEP" = "syn" ] || [ "$STEP" = "package" ]; then
  # synthesis must precede co-sim / packaging; v++ --mode hls runs C synthesis
  v++ -c --mode hls --config "$CFG" --work_dir "$WS/work" 2>&1 | tee "$WS/syn.log" | grep -E 'ERROR|WARNING: \[HLS|Finished|Estimated|error' | head -20 || true
fi
if [ "$STEP" != "syn" ]; then
  vitis-run --mode hls $FLAG --config "$CFG" --work_dir "$WS/work" 2>&1 | tee "$WS/$STEP.log" | tail -n "${TAIL:-40}"
fi
