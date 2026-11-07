#!/usr/bin/env bash
#
# collect_ip.sh - Unpack the packaged HLS IPs into the Vivado IP repository.
#
# Looks for workspace/vitishls/<core>_build/work/<core>.zip (made by run_hls.sh <core> package)
# and unpacks each into workspace/vivado/ip_repo/<core>/ (untracked).
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
REPO="$ROOT/workspace/vivado/ip_repo"
mkdir -p "$REPO"
for core in vc_filter vc_overlay vc_snapshot; do
  zip="$ROOT/workspace/vitishls/${core}_build/work/${core}.zip"
  [ -f "$zip" ] || { echo "missing $zip - run: src/cores/vc_common/run_hls.sh $core package" >&2; exit 1; }
  rm -rf "${REPO:?}/$core"; mkdir -p "$REPO/$core"
  unzip -q -o "$zip" -d "$REPO/$core"
  echo "unpacked $core"
done
