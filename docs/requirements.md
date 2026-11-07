# vidcap_pynqz2 - Requirements and design (v1.0.0)

Status: **v1.0.0.** Everything below is implemented and verified in simulation and by a timing-clean
Vivado build. It has also been run on a PYNQ-Z2 with the built-in test pattern as the only video source
(section 5). **A real HDMI source and a monitor have not been tried.**

## 1. Goal

HDMI video in → programmable video processing → HDMI video out on a PYNQ-Z2 (Zynq-7020), controlled
from Jupyter via PYNQ. Fixed format: **1280x720 @ 60 Hz, 24-bit RGB**.

## 2. Architecture

```
                                  aclk = 100 MHz for everything between the two video-IP boxes
 HDMI RX → dvi2rgb → v_vid_in_axi4s ─┐                                   ┌─ v_axi4s_vid_out → rgb2dvi → HDMI TX
        (recovered clock)  (clock crossing)                               (clock crossing)  (74.25 MHz pixel clock)
                                     │                                   │
   test pattern (vc_tpg) ──┐         ▼                                   │
                           └─► vc_input_sel ─► vc_snapshot ─► VDMA live S2MM ─► DDR [live ring x3]
                              (select, pace,    (in series;                                    │
                               fallback, frame   writes 1 frame                                ▼
                               normaliser)       to DDR on trigger)          VDMA live MM2S ──┐
                                                                                                ├─► vc_stream_mux ─► vc_tavg ─► vc_filter ─► vc_overlay ─► swap ─┐
                                              DDR [video memory x2] ─► VDMA vmem MM2S ──────────┘   (live/vmem) (temporal) (8 modes)      (timestamp)         │
                                                                                                                                                    ▼
                                                                          vc_rtc (hardware clock) ───────────────────► time digits ─► vc_overlay      v_axi4s_vid_out
```

Control: PS → AXI-Lite → a register block on every core. Interrupts: AXI INTC → PS `IRQ_F2P[0]`.

### Design decisions that differ from the first draft of this spec

| Decision | Why |
|---|---|
| **One stable 100 MHz clock** for snapshot, VDMAs, filter, overlay and all registers; the only clock crossings are inside Xilinx's `v_vid_in_axi4s` and `v_axi4s_vid_out`. | No logic depends on a clock that can disappear (cable pulled), so the planned `BUFGCTRL` clock switch was unnecessary. |
| **Snapshot sits in series** in the stream, not on a broadcast branch. | A broadcaster stalls both outputs if one stops accepting data, and an idle HLS core does not accept data. In series, an idle snapshot just passes pixels through. |
| **Own test-pattern generator** (`vc_tpg`, RTL) instead of Xilinx `v_tpg`. | Fixed colour order, no software configuration, deterministic per frame, fully simulated. |
| **Video memory has its own read-only VDMA** plus a frame-aligned mux (`vc_stream_mux`). | A parked read channel that must also genlock to the writer is a corner that cannot be proven without hardware; two readers have plain, documented semantics. Only the selected reader moves data. |
| **One filter core** (`vc_filter`) with a mode register, not seven cores behind a stream switch. | One 3x3 window pipeline serves every mode, the mode is latched at the start of each frame (so a change never lands mid-frame), and it costs 3,000 LUTs. |
| **Pixel format `{R,G,B}`** inside the pipeline; swapped once at each HDMI edge. | The Digilent HDMI IP uses `{R,B,G}`. In memory the bytes are B, G, R (OpenCV / PYNQ order). |

## 3. Requirements

### Input
- **REQ-IN-1** Accept 1280x720@60 RGB888 HDMI. The receiver's EDID advertises 720p only (`720p_edid.data`).
- **REQ-IN-2** Report to software: locked, measured size, frame counts.
- **REQ-IN-3** Any input that is absent or not 1280x720 is "no valid input".
- **REQ-IN-4** Software selects the source: external HDMI or the internal test pattern.
- **REQ-IN-5** With HDMI selected and no valid input for **5 s**, switch to the test pattern; return after
  the input has been valid for 1 s. Software can read the state, change both times, and disable it.
- **REQ-IN-6** The snapshot core and the frame buffer see the *selected* source, so the whole pipeline can
  be exercised with no HDMI source.
- **REQ-IN-7** Test pattern: 8 colour bars with a moving box (8 px right, 4 px down per frame, bouncing).

### Snapshot
- **REQ-SNAP-1** Taps the raw selected input, before the frame buffer and any filter.
- **REQ-SNAP-2** On trigger, stores the next complete frame, then raises an interrupt. Never a partial frame.
- **REQ-SNAP-3** Packed RGB888 (3 bytes/pixel, B,G,R), 2,764,800 bytes, at an address software provides.
- **REQ-SNAP-4** Status: busy, done count. Interrupt clears when software acknowledges.

### Buffering
- **REQ-BUF-1** Input frames are written to a triple-buffered ring in DDR; the output reads independently
  (genlock keeps the reader off the frame being written). A rate mismatch repeats or drops whole frames.

### Filters
- **REQ-FLT-1** Modes: passthrough, invert, grayscale, threshold, Sobel, blur, sharpen, noise.
- **REQ-FLT-2** Passthrough is a real mode and the power-up default.
- **REQ-FLT-3** Mode and threshold are AXI-Lite registers, sampled at the start of each frame.
- **REQ-FLT-4** Output is always 1280x720 RGB888 (borders replicated, no size change).
- **REQ-FLT-5** Filters apply to everything displayed: live video, test pattern, video memory.

- **REQ-FLT-6** Noise mode adds retro-VCR grain from a free-running LFSR. Strength is a register (0 = none, 255 = up to
  +-127); the same offset is applied to all three channels (grey snow); the pattern differs in every frame.

### Temporal average
- **REQ-TAVG-1** Average the brightness of the last N frames, N programmable 1..255 and limited by the history the
  software reserves. Colour is taken from the current frame.
- **REQ-TAVG-2** Use a running sum (`S = S + new - oldest`), so DDR traffic is constant, about 330 MB/s at 720p60, for any N.
  The sum is exact integer arithmetic and cannot drift.
- **REQ-TAVG-3** While fewer than N frames are available (after switching on, or after N changes) it averages what it has;
  the first output frame is the input unchanged. A constant picture comes out exactly unchanged for every N.
- **REQ-TAVG-4** Off by default; when off it passes video through and generates no memory traffic. All controls take effect
  on a frame boundary.
- **REQ-TAVG-5** It combines with any filter (it runs before the filter).
- *Not supported:* an arbitrary 60-frame FIR. A general N-tap temporal FIR must read N history frames per output frame, about
  N x 55 MB/s even for brightness only, which exceeds the memory bandwidth beyond roughly 14 taps at 60 fps. The running
  sum gives any N for equal weights.

### Video memory
- **REQ-VMEM-1** Software writes a 1280x720 RGB888 image to DDR and selects it for display.
- **REQ-VMEM-2** Double-buffered: write the hidden buffer, flip the park pointer, no tearing.
- **REQ-VMEM-3** Video memory passes through the filters and the timestamp.

### Output
- **REQ-OUT-1** Free-running 1280x720@60 output timing from a local 74.25 MHz clock.
- **REQ-OUT-2** The output keeps running in all cases (a monitor stays locked).
- **REQ-OUT-3** Software selects live video or video memory; the test pattern is selected on the input side.

### Timestamp
- **REQ-TS-1** A hardware clock holds date and time; software sets it from the Linux clock.
- **REQ-TS-2** Displayed as `YYYY-MM-DD HH:MM:SS` in the bottom-right corner, 8x16 font, scale 1-4.
- **REQ-TS-3** Drawn after the filter (never filtered), over every source, with an enable bit.
- **REQ-TS-4** Atomic set (shadow + load); consistent read-back; drift is measurable and trimmable.
- **REQ-TS-5** The clock does calendar rollover itself, including leap years and the century rule.
- **REQ-TS-6** Powers up at **2026-01-01 00:00:00**.

### Software
- **REQ-SW-1** v1 is driven from Jupyter through the PYNQ overlay (`src/drivers`, `src/notebooks`).
- **REQ-SW-2** Nothing is reachable only through PYNQ: documented register map (`docs/register_map.md`),
  standard AXI-Lite and AXI VDMA, driver classes are thin register wrappers, interrupts are one AXI INTC.
  *Partly met:* on PYNQ the frame buffers come from PYNQ's allocator, so their addresses change between
  runs. The driver also supports a reserved region at fixed addresses (`ReservedRegion`), which a non-PYNQ
  client should use; the device-tree reservation itself is not part of v1.

## 4. Traceability

"Verified by" names the test that exists in this repository. Section 5 lists what the board run adds.

| Requirement | Implemented in | Verified by (simulation / build) |
|---|---|---|
| IN-1, IN-2 | `dvi2rgb` (720p EDID), `v_vid_in_axi4s`, `vc_input_sel` (measures size/frames) | `tb_vc_input_sel` T2 (measured 8x4) |
| IN-3, IN-5 | `vc_input_sel` | `tb_vc_input_sel` T3, T4, T8, T9 (timeout, return, lock loss, disabled) |
| IN-4, IN-6 | `vc_input_sel` | `tb_vc_input_sel` T1, T7 |
| IN-7 | `vc_tpg` | `tb_vc_tpg` (bars, box step, bounce) |
| SNAP-1..4 | `vc_snapshot` | `vc_snapshot` C-sim (7 cases) + C/RTL co-simulation |
| BUF-1 | `vdma_live`, `Vdma` driver | register sequence in driver tests only |
| FLT-1..4 | `vc_filter` | C-sim: 8 modes x 2 images vs golden model, 2-frame, junk-before-SOF; co-simulation |
| FLT-6 | `vc_filter` (mode 7) | C-sim vs a golden LFSR model: amounts 0/1/40/255, grain differs per frame, grey noise; co-simulation |
| TAVG-1..5 | `vc_tavg` | C-sim vs a golden model built from a plain list of frames (warm-up, N changes, ring wrap, N = ring size, clamping, restarts, disable/enable, exact constant picture) plus a check of the ring and sum frame in memory after every frame; a deliberate fault is caught; RTL co-simulation |
| FLT-5, VMEM-3 | `vc_stream_mux` ahead of `vc_filter` | block design; `tb_vc_stream_mux` |
| VMEM-1, 2 | `vdma_vmem`, `vc_stream_mux`, `Vidcap.show_image` | `tb_vc_stream_mux` (frame-aligned switch), driver tests |
| OUT-1, 2 | `v_tc` (fixed 720p), `v_axi4s_vid_out`, `clk_wiz` (74.25 MHz exact) | timing-clean implementation |
| OUT-3 | `vc_stream_mux`, `vc_input_sel` | `tb_vc_stream_mux`, `tb_vc_input_sel` |
| TS-1..6 | `vc_rtc`, `vc_overlay` | `tb_vc_rtc` (14 checks: leap years, 1900/2000, rollovers, bad dates, CPS trim, power-up); `vc_overlay` C-sim + co-simulation |
| SW-1, SW-2 | `src/drivers`, `docs/register_map.md` | 25 driver unit tests, including a check that every HLS register offset matches the generated headers |

## 5. Resources, timing and board results (Vivado 2025.2, xc7z020clg400-1)

Implementation (with the noise mode and the temporal average): 13,580 LUTs (26%), 19,272 registers (18%), 32.5 BRAM tiles
(23%), 39 DSPs (18%); setup slack +0.68 ns, hold slack +0.013 ns. The temporal-average core is about 6.5k LUTs, 18 BRAM tiles and
7 DSPs, and processes a frame in 9.46 ms. See `workspace/vivado/out/` after `make overlay`.
The build **fails** if setup or hold timing is not met.

**On the board** (`src/drivers/tests/hw_smoke.py`, test pattern as the source, 33 of 33 checks pass):

| Verified on hardware | Evidence |
|---|---|
| Bitstream loads; `rtc`, `input_sel`, `stream_mux` answer with the right IDs | first AXI reads |
| Test pattern flows through input select, snapshot, VDMA write and read, mux | 60 frames/s measured at both input select and mux |
| DMA writes correct frames to the live ring | all 3 frame buffers hold the colour bars |
| Snapshot (REQ-SNAP) | 720x1280x3; bars in the right order and **B,G,R byte order**; box moves between captures; repeated captures; interrupt acknowledge; about 40 ms |
| Video memory (REQ-VMEM-1, 2) | mux switches to it, reader delivers 60 frames/s, second buffer flip accepted, no VDMA errors |
| Hardware clock (REQ-TS-1, 4) | within 0.3-0.8 s of the board clock after a set and 2 s |
| Fallback (REQ-IN-3, 5) | HDMI reported not good with nothing connected; test pattern took over after 5 s; output kept running |
| Stability | board did not reset across two bitstream loads and the runs |
| Noise (REQ-FLT-6) | mode and amount register accepted and read back; video stays at 60 fps at full noise (the grain itself is only visible on a monitor) |
| Temporal average (REQ-TAVG) | window fills to N=16; all 16 history slots hold the brightness of the colour bars; the sum frame holds exactly 16 x brightness (142 of 144 sampled pixels, the rest under the moving box); 60 fps with it on; changing N restarts it; switching off works; no VDMA errors |

One start-up behaviour was found and handled: the write channel flags a one-time `internal error / SOF late` because
video is already flowing when it starts. The flags do not return once cleared (checked over 2 s, about 120 frames),
and `Vidcap.start()` now clears them so `status()` only shows later errors.

## 6. Known limits and things to confirm on hardware

1. **Not yet tried with a real HDMI source or a monitor.** Everything on the HDMI connectors, and what the picture
   looks like, is unverified (see README "First run").
2. **VDMA programming** now runs on the board without errors, but only with the test pattern.
3. **Video-memory flip**: the reader runs and the flip is accepted; that the displayed image switches without
   tearing has not been looked at on a monitor.
4. **HDMI output hot-plug pin** (`hdmi_out_hpd`, R19) is driven high, as in PYNQ's base design.
5. **Colour order**: the internal `{R,G,B}` path and the B,G,R byte order in memory are confirmed on the board
   (snapshot colour bars). The two swaps at the HDMI edges (`vc_input_sel` on input, `vc_axis_swap_gb` on output)
   are derived from the IP source and PYNQ's `color_swap` configuration, and need an HDMI source and a monitor to confirm.
6. **Interrupts** are wired (HLS snapshot + three VDMA lines → AXI INTC → PS) but the Python drivers poll.
7. **Clock drift** of the timestamp is that of the board's 50 MHz PS oscillator; use `Rtc.cycles_per_second`.
8. **A source that ignores the 720p EDID** (for example 1080p) gives no lock; the board falls back to the
   test pattern after 5 s.
