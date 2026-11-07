# vidcap_pynqz2

**Version 1.1.0** (see `VERSION`)

HDMI video capture, live filtering and display for the **PYNQ-Z2** (Zynq-7020), controlled from Jupyter.

```
HDMI in ─► [test pattern / HDMI select] ─► snapshot ─► DDR ─► filter ─► timestamp ─► HDMI out
                                                          ▲
                                         video memory ────┘ (show any 1280x720 image instead)
```

* **Format:** 1280x720 @ 60 Hz, 24-bit colour.
* **Snapshot:** software-triggered capture of one complete frame into DDR, with an interrupt.
* **Filters** (switchable per frame): passthrough, invert, grayscale, threshold, Sobel edges, blur, sharpen, and **noise**
  (retro-VCR grain from an LFSR, adjustable strength).
* **Temporal average:** brightness averaged over the last N frames (N programmable, up to the history you reserve), using a
  running sum in DDR so the memory cost is the same for any N. Combines with any filter.
* **Video memory:** double-buffered display of any image you write, also filtered.
* **Timestamp:** date and time drawn by a hardware clock (leap years included), set from the board's clock.
* **Test pattern:** colour bars with a moving box. Selectable by software, and automatic if the HDMI
  input is missing or not 720p for 5 seconds, so the monitor never goes blank.

## Status: verified in simulation and on the board (test pattern); HDMI in/out not yet tried

| What | State |
|---|---|
| RTL blocks (clock, input select, mux, test pattern) | simulated with self-checking testbenches (Vivado xsim) |
| HLS cores (filter, overlay, snapshot) | C-simulation against golden models **and** C/RTL co-simulation |
| Python drivers | 25 unit tests against a fake register file, including a check that every register offset matches what the HLS tools generated |
| Full design | builds from source; timing met (setup +0.68 ns, hold +0.013 ns); 26% LUTs, 23% BRAM, 18% DSPs |
| **On the board** (`hw_smoke.py`, test pattern as the source) | 33 of 33 checks pass: bitstream loads, every block answers, test pattern at 60 fps through the DMA ring, snapshot returns the colour bars in the right byte order, video-memory flip, hardware clock, noise setting, **temporal average** (window fills to N, the history ring and the 16-bit sum frame in DDR hold exactly the expected values, 60 fps with it on, N change restarts it), fallback |
| **Not yet verified** | what the picture looks like: the grain, the averaged brightness, the filters, the timestamp text and the HDMI colour-order swaps all need a monitor; a live HDMI source through the new filters; interrupts |

The board check can be repeated at any time (needs root and the PYNQ environment):

```
cd /home/xilinx/jupyter_notebooks/vidcap
sudo bash -c 'source /etc/profile.d/boardname.sh /etc/profile.d/xrt_setup.sh /etc/profile.d/pynq_venv.sh; python3 -u hw_smoke.py'
```

The "First run" section below lists what to check when you connect HDMI, and `docs/requirements.md` section 6
lists what is still unproven.

## Quick start (on the board)

1. Power the board from a **proper 5 V supply** (barrel jack, with the power-source jumper JP5 on REG), not
   just a laptop USB port. Vivado estimates about 2 W for the chip alone, which is at the limit of what USB
   can supply to the whole board, and a weak supply makes the board reset during boot.
2. Put the board and your computer on one network (for example with the helper that shares your Wi-Fi over
   the wired port) and copy the package over:
   ```
   make deploy BOARD=xilinx@<board-ip>          # builds the package and scp's it (asks for the password)
   ```
3. Open Jupyter at `http://<board-ip>:9090`, go to `vidcap/`, and open **`vidcap_interactive.ipynb`** for a
   control panel (source, filters, timestamp, snapshot, video memory, live status), or **`vidcap_demo.ipynb`**
   for the same features as plain Python.

Or from any Python on the board:

```python
from vidcap import Vidcap
v = Vidcap.on_pynq('vidcap.bit')     # loads the bitstream
v.start()                            # DMA engines, live video, clock set from the board
v.set_filter('sobel')
img = v.snapshot()                   # (720, 1280, 3) uint8, B,G,R order
v.show_image(img)                    # show it instead of live video
v.show_live()
v.status()
```

**Time zone:** the on-screen clock shows the board's local time, and the board defaults to UTC. `make deploy` sets
the board's zone to your computer's (override with `TZ_NAME=America/Chicago`, or `TZ_NAME=` to leave it alone). To show
another zone without touching the board, use `v.start(tz="Europe/London")` or `v.sync_time(tz=...)`, or the **Time zone**
box in the control panel. The hardware clock keeps plain wall time, so press **Set clock** (or call `sync_time()`)
after a daylight-saving change.

Plug an HDMI source (720p) into the **input** connector and a monitor into the **output** connector.
The input advertises a 720p-only EDID, so a computer or camera will pick 1280x720 by itself.

## First run: what to check

| Symptom | Likely cause |
|---|---|
| `unexpected ID ... is the vidcap bitstream loaded?` | `vidcap.bit` and `vidcap.hwh` are not next to each other, or another overlay is loaded |
| Board resets during boot | power supply (see Quick start) |
| No picture, LD1/LD2 on | HDMI input is not "good": check `v.input.status()` (locked? size 1280x720?) |
| Monitor shows colour bars | test pattern is active: no valid HDMI for 5 s, or `use_test_pattern(True)` |
| Picture but wrong colours | channel order: bars should read white, yellow, cyan, green, magenta, red, blue, black |
| Frozen or torn picture | `v.status()` shows VDMA error bits (SOF/EOL early/late, slave/decode errors) |
| `stream mux did not switch` | the selected reader is not producing frames (VDMA not running) |
| Picture frozen, snapshots never finish, `v.snapshot()` times out | run `sudo python3 -m vidcap.diagnose` in the `vidcap/` folder (read-only). If it says STALLED and a DMA reset times out, the board's memory port is wedged: shut down the notebooks and **reboot the board** (a bitstream reload does not clear it). Use one notebook at a time |

Status LEDs: **LD0** HDMI good, **LD1** fallback active, **LD2** test pattern selected, **LD3** output clock locked.

## Repository layout

```
src/                   everything that is hand-written (this is what is in git)
  cores/               hardware, one folder per core, each with its tests
    vc_rtc/              hardware date/time clock                 (Verilog)
    vc_input_sel/        HDMI/test select, fallback, frame check  (Verilog)
    vc_stream_mux/       live / video-memory select               (Verilog)
    vc_tpg/              colour bars + moving box                 (Verilog)
    vc_axis_swap/        HDMI colour-order swap                   (Verilog)
    vc_filter/           7 filter modes                           (HLS C++)
    vc_overlay/          timestamp text                           (HLS C++)
    vc_snapshot/         single-frame capture                     (HLS C++)
    vc_common/           shared AXI-Lite bridge, video header, run_xsim.sh, run_hls.sh
  overlay/             Vivado block design (Tcl), pin constraints, build scripts
  drivers/vidcap/      Python drivers (no PYNQ needed for the logic) and the ipywidgets panel (ui.py)
  drivers/tests/       driver unit tests
  notebooks/           interactive control panel and plain-Python demo
docs/
  requirements.md      requirements, architecture, traceability, known limits
  register_map.md      every register of every block: the hardware contract
scripts/               generators for standalone Tcl (HLS workspaces, Vivado project, IP catalogue)
workspace/             everything generated (NOT in git): Vivado projects, HLS runs, bitstream, logs
  vivado/  vitishls/
Makefile  VERSION  README.md
```

`workspace/` is deliberately untracked, so every file in it can be regenerated from `src/` with `make`.

## Building

Needs Vivado/Vitis **2025.2** (looked for in `~/AMD/2025.2`; set `AMD_ROOT` otherwise) and internet access once.

```
make test          # all RTL simulations, HLS C-simulations, driver tests            (~5 min)
make test-cosim    # HLS C/RTL co-simulation                                         (~10 min)
make overlay       # HLS -> IP -> Vivado: workspace/vivado/out/vidcap.bit + .hwh      (~15 min)
make dist          # package for the board: workspace/dist/vidcap/
make deploy BOARD=xilinx@<ip>
make gen-tcl       # write standalone Tcl that recreates the HLS and Vivado workspaces and IP
make clean         # delete generated files (the folder layout stays)
```

`scripts/` holds the Tcl generators (see `scripts/README.md`); the Tcl they write goes under `workspace/`.

The build **fails** if timing is not met. `make overlay` fetches PYNQ's official PYNQ-Z2 settings and the
Digilent HDMI IP at a pinned tag (`src/overlay/scripts/fetch_refs.sh`) instead of copying them into this repo.

## Using it without PYNQ

`docs/register_map.md` is the complete hardware contract: base addresses, every register, the video
format and the start-up sequence. The drivers keep register access behind a two-method interface
(`read` / `write`) with PYNQ, `/dev/mem` and fake back-ends, and frame memory can be a reserved region at
fixed addresses (`ReservedRegion`). That is the intended route for a future Rust client.

## Third-party material

* PYNQ-Z2 board settings and the Digilent `dvi2rgb` / `rgb2dvi` IP are fetched from the PYNQ repository
  (tag `v3.1`) at build time and are not stored here; they keep their own licences.
* The timestamp font is generated from DejaVu Sans Mono (`src/cores/vc_overlay/gen_font.py`).

No licence has been chosen for this repository's own code yet.
