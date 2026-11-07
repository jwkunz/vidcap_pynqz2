# vidcap_pynqz2 - top-level build and test.
#
# Sources live in src/; every generated file goes under workspace/ (untracked).
#
#   make test        all simulations and unit tests (no hardware)
#   make overlay     build the bitstream from scratch (HLS cores -> IP -> Vivado)
#   make dist        collect what goes on the board into workspace/dist/vidcap/
#   make deploy BOARD=xilinx@192.168.2.82     copy dist to the board (asks for the board password)
#   make clean       delete everything generated (the workspace folder layout stays)
#
# Needs Vivado/Vitis 2025.2 (default ~/AMD/2025.2, override with AMD_ROOT) and internet access for the
# one-time fetch of the PYNQ-Z2 reference files.

SHELL     := /bin/bash
ROOT      := $(CURDIR)
CORES_HLS := vc_filter vc_overlay vc_snapshot
SIM       := src/cores/vc_common/run_xsim.sh
HLS       := src/cores/vc_common/run_hls.sh
OUT       := workspace/vivado/out
DIST      := workspace/dist/vidcap
BOARD     ?= xilinx@192.168.2.82

.PHONY: all test test-rtl test-hls test-cosim test-drivers refs hls-package ip overlay dist deploy clean version

all: test overlay dist

version:
	@cat VERSION

# ------------------------------------------------------------------ tests
test: test-rtl test-hls test-drivers

test-rtl:
	$(SIM) tb_vc_rtc          src/cores/vc_common/vc_axil_bridge.v src/cores/vc_rtc/vc_rtc.v src/cores/vc_rtc/tb/tb_vc_rtc.v
	$(SIM) tb_vc_input_sel    src/cores/vc_common/vc_axil_bridge.v src/cores/vc_input_sel/vc_input_sel.v src/cores/vc_input_sel/tb/tb_vc_input_sel.v
	$(SIM) tb_vc_tpg          src/cores/vc_tpg/vc_tpg.v src/cores/vc_tpg/tb/tb_vc_tpg.v
	$(SIM) tb_vc_stream_mux   src/cores/vc_common/vc_axil_bridge.v src/cores/vc_stream_mux/vc_stream_mux.v src/cores/vc_stream_mux/tb/tb_vc_stream_mux.v

test-hls:
	@for c in $(CORES_HLS); do echo "== csim $$c"; $(HLS) $$c csim | grep -E 'TEST (PASSED|FAILED)' || exit 1; done

test-cosim:
	@for c in $(CORES_HLS); do echo "== cosim $$c"; $(HLS) $$c cosim | grep -E 'co-simulation finished' || exit 1; done

test-drivers:
	python3 -I -W error -m unittest discover -s src/drivers/tests

# ------------------------------------------------------------------ bitstream
refs:
	src/overlay/scripts/fetch_refs.sh

hls-package:
	@for c in $(CORES_HLS); do echo "== package $$c"; $(HLS) $$c package >/dev/null || exit 1; done

ip: hls-package
	src/overlay/scripts/collect_ip.sh

overlay: refs ip
	# run from inside workspace/ so Vivado scratch files (.Xil, NA/) never land in the source tree
	source $${AMD_ROOT:-$$HOME/AMD}/2025.2/Vitis/settings64.sh >/dev/null 2>&1; \
	cd workspace/vivado && vivado -mode batch -nolog -nojournal -source $(ROOT)/src/overlay/scripts/build.tcl -tclargs $(ROOT) bit

# ------------------------------------------------------------------ packaging for the board
dist:
	@test -f $(OUT)/vidcap.bit || { echo "no bitstream: run 'make overlay' first"; exit 1; }
	rm -rf $(DIST)
	mkdir -p $(DIST)
	cp $(OUT)/vidcap.bit $(OUT)/vidcap.hwh $(DIST)/
	cp -r src/drivers/vidcap $(DIST)/vidcap
	cp -r src/notebooks/* $(DIST)/
	cp VERSION $(DIST)/
	find $(DIST) -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "board package in $(DIST)"

deploy: dist
	scp -r $(DIST) $(BOARD):/home/xilinx/jupyter_notebooks/

# Removes only generated output under workspace/, never the placeholder files that keep the layout.
clean:
	find workspace -mindepth 2 -type f ! -name .gitkeep -delete
	find workspace -mindepth 2 -type d -empty -delete
