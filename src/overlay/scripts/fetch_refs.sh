#!/usr/bin/env bash
#
# fetch_refs.sh - Fetch third-party reference files (pinned) into workspace/vivado/refs.
#
# Pulls the official PYNQ-Z2 base design (pin constraints, PS7 settings) and the
# Digilent dvi2rgb / rgb2dvi HDMI IP from the PYNQ repository at a fixed tag, so the
# build is reproducible and nothing third-party is stored in this repo's git history.
#
set -euo pipefail

PYNQ_TAG="${PYNQ_TAG:-v3.1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DEST="${REFS_DIR:-$ROOT/workspace/vivado/refs}"

# interface definitions (tmds_rtl etc.) live in boards/ip/if
PATHS="boards/Pynq-Z2/base boards/ip/dvi2rgb_v1_7 boards/ip/rgb2dvi_v1_2 boards/ip/if"

if [ ! -d "$DEST/PYNQ/.git" ]; then
  mkdir -p "$DEST"
  git clone --depth 1 --filter=blob:none --sparse --branch "$PYNQ_TAG" \
    https://github.com/Xilinx/PYNQ.git "$DEST/PYNQ"
fi
# always (re)apply, so adding a path above takes effect on an existing checkout
# shellcheck disable=SC2086
git -C "$DEST/PYNQ" sparse-checkout set $PATHS
echo "fetched PYNQ $PYNQ_TAG ($(git -C "$DEST/PYNQ" rev-parse --short HEAD)) into $DEST/PYNQ"

# patched local copy of the Digilent HDMI IP (extra clock bands and the 480p/240p EDIDs)
python3 "$ROOT/src/overlay/scripts/prepare_ip.py"
