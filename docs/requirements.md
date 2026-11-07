# vidcap_pynqz2 - Requirements and design (v1.0.0)

Status: **v1.0.0.** Everything below is implemented and verified in simulation and by a timing-clean
Vivado build. **Nothing has yet been run on the board**; the "Hardware" column in section 4 says so
requirement by requirement.

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
                                                                                                ├─► vc_stream_mux ─► vc_filter ─► vc_overlay ─► swap ─┐
                                              DDR [video memory x2] ─► VDMA vmem MM2S ──────────┘   (live/vmem)     (7 modes)      (timestamp)         │
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
- **REQ-FLT-1** Modes: passthrough, invert, grayscale, threshold, Sobel, blur, sharpen.
- **REQ-FLT-2** Passthrough is a real mode and the power-up default.
- **REQ-FLT-3** Mode and threshold are AXI-Lite registers, sampled at the start of each frame.
- **REQ-FLT-4** Output is always 1280x720 RGB888 (borders replicated, no size change).
- **REQ-FLT-5** Filters apply to everything displayed: live video, test pattern, video memory.

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

"Verified by" names the test that exists in this repository. **Hardware: none of these have run on the board.**

| Requirement | Implemented in | Verified by (simulation / build) |
|---|---|---|
| IN-1, IN-2 | `dvi2rgb` (720p EDID), `v_vid_in_axi4s`, `vc_input_sel` (measures size/frames) | `tb_vc_input_sel` T2 (measured 8x4) |
| IN-3, IN-5 | `vc_input_sel` | `tb_vc_input_sel` T3, T4, T8, T9 (timeout, return, lock loss, disabled) |
| IN-4, IN-6 | `vc_input_sel` | `tb_vc_input_sel` T1, T7 |
| IN-7 | `vc_tpg` | `tb_vc_tpg` (bars, box step, bounce) |
| SNAP-1..4 | `vc_snapshot` | `vc_snapshot` C-sim (7 cases) + C/RTL co-simulation |
| BUF-1 | `vdma_live`, `Vdma` driver | register sequence in driver tests only |
| FLT-1..4 | `vc_filter` | C-sim: 7 modes x 2 images vs golden model, 2-frame, junk-before-SOF; co-simulation |
| FLT-5, VMEM-3 | `vc_stream_mux` ahead of `vc_filter` | block design; `tb_vc_stream_mux` |
| VMEM-1, 2 | `vdma_vmem`, `vc_stream_mux`, `Vidcap.show_image` | `tb_vc_stream_mux` (frame-aligned switch), driver tests |
| OUT-1, 2 | `v_tc` (fixed 720p), `v_axi4s_vid_out`, `clk_wiz` (74.25 MHz exact) | timing-clean implementation |
| OUT-3 | `vc_stream_mux`, `vc_input_sel` | `tb_vc_stream_mux`, `tb_vc_input_sel` |
| TS-1..6 | `vc_rtc`, `vc_overlay` | `tb_vc_rtc` (14 checks: leap years, 1900/2000, rollovers, bad dates, CPS trim, power-up); `vc_overlay` C-sim + co-simulation |
| SW-1, SW-2 | `src/drivers`, `docs/register_map.md` | 25 driver unit tests, including a check that every HLS register offset matches the generated headers |

## 5. Resources and timing (Vivado 2025.2, xc7z020clg400-1)

See `workspace/vivado/out/` after `make overlay` (`utilization_impl.rpt`, `timing_summary.rpt`).
The build **fails** if setup or hold timing is not met.

## 6. Known limits and things to confirm on hardware

1. **Not yet run on the board.** Expect bring-up debugging (see README "First run").
2. **VDMA register programming** follows Xilinx PG020 and PYNQ's static genlock pairing (S2MM master,
   MM2S slave). It is unproven here; `Vidcap.status()` reports the VDMA error bits to help.
3. **Video-memory flip** relies on the VDMA applying a park-pointer write at a frame boundary.
4. **HDMI output hot-plug pin** (`hdmi_out_hpd`, R19) is driven high, as in PYNQ's base design.
5. **Colour order** (`{R,B,G}` at the Digilent IP, swapped in `vc_input_sel` and `vc_axis_swap_gb`) is
   derived from the IP source and PYNQ's own `color_swap` configuration; verify with the colour bars.
6. **Interrupts** are wired (HLS snapshot + three VDMA lines → AXI INTC → PS) but the Python drivers poll.
7. **Clock drift** of the timestamp is that of the board's 50 MHz PS oscillator; use `Rtc.cycles_per_second`.
8. **A source that ignores the 720p EDID** (for example 1080p) gives no lock; the board falls back to the
   test pattern after 5 s.
