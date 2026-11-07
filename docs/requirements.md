# vidcap_pynqz2 - Requirements (draft 3)

Status: **draft 3, 2026-11-07**, from the coding interview. Items marked **[ASSUMED]**
are my inference and need confirming. Items in section 9 are still open.

Changes in draft 3: test pattern defined (colour bars plus a moving element);
automatic fallback confirmed with a 5 second timeout; snapshot is never timestamped.
Changes since draft 1: video memory now passes through the filters (single read
channel, no output mux); the test-pattern generator moved to the input side with a
software source select; the time counter powers up at 2026-01-01 00:00:00.

## 1. Goal

HDMI video in -> programmable video processing -> HDMI video out on a PYNQ-Z2
(Zynq-7020), controlled from Jupyter via PYNQ. Fixed format: **1280x720 @ 60 Hz,
RGB888**.

## 2. Architecture

```
 INPUT CLOCK DOMAIN                                  |  OUTPUT CLOCK DOMAIN
 (switchable: recovered HDMI clock or local 74.25)   |  (local, free-running 74.25 MHz)
                                                     |
 HDMI RX -> dvi2rgb --+                              |
                      +--> Input mux (sw) --+--> Snapshot core --AXI-MM--> DDR [snapshot] --irq--> PS
 Test-pattern gen ----+                      |       (sw trigger)
                                             +--> VDMA S2MM --> DDR [input ring x3]
                                                                         |
                          DDR [video memory x2] -------------------------+--> VDMA MM2S (sw picks source)
                                                                                  |
                                                                                  v
                                       Filter switch -> filters -> Timestamp overlay -> VTC -> rgb2dvi -> HDMI TX
```

Control path: PS -> AXI-Lite -> registers on every block. Everything above is a
standard Xilinx IP block except the snapshot core, the filters, the filter switch,
and the timestamp overlay.

## 3. Functional requirements

### Input
- **REQ-IN-1** Accept 1280x720@60 RGB888 HDMI.
- **REQ-IN-2** Report to software: signal locked, measured resolution/refresh.
- **REQ-IN-3** Any HDMI input that is absent or not 720p60 is "no valid input".
- **REQ-IN-4** Software selects the input source: **external HDMI** or **internal test
  pattern**.
- **REQ-IN-5** With the source set to HDMI and no valid HDMI input (REQ-IN-3) continuously
  for **5 seconds**, the design switches to the test pattern automatically so the output
  never stays blank. When a valid input has been present again for **[ASSUMED]** 1 second,
  it switches back. Software can read a status bit saying the fallback is active, and can
  turn the fallback off. The 5 second timeout is a register, with 5 s as its default.
- **REQ-IN-7** Test pattern: **colour bars with a moving element** (e.g. a box or bar
  that travels across the frame every frame), so a frozen or frame-dropping pipeline is
  obvious on screen and in snapshots.
- **REQ-IN-6** The selected source (HDMI or test pattern) is what the snapshot core and
  the frame buffer see, so the whole pipeline can be exercised without an HDMI source.

### Snapshot (software-triggered capture)
- **REQ-SNAP-1** Taps the **selected input** stream (HDMI or test pattern), before the
  frame buffer and before any filtering.
- **REQ-SNAP-2** On software trigger, wait for the next frame start, store exactly one
  complete frame, then raise an interrupt. Never a partial or torn frame.
- **REQ-SNAP-3** Stored as packed RGB888 (3 bytes/pixel, 2,764,800 bytes) at a fixed
  physical address.
- **REQ-SNAP-4** Status register: idle / armed / capturing / done / error. The
  interrupt is cleared by software.

### Frame buffering
- **REQ-BUF-1** Input frames are written to DDR by VDMA S2MM, triple-buffered. The
  output side reads independently, so a rate mismatch drops or repeats whole frames only.

### Filters (streaming HLS cores in a software-configured switch)
- **REQ-FLT-1** Modes for v1: **passthrough, invert, grayscale, threshold, Sobel edge
  detect, blur, sharpen.**
- **REQ-FLT-2** **Passthrough** is a real, selectable mode that leaves the video
  unchanged. **[ASSUMED]** It is also the power-up default.
- **REQ-FLT-3** Mode and parameters (e.g. threshold level, sharpen amount) are set over
  AXI-Lite. **[ASSUMED]** A mode change takes effect at a frame boundary, with no
  partial-frame mixing.
- **REQ-FLT-4** Every mode outputs the same format it takes in: 1280x720 RGB888, no
  change in frame size or rate.
- **REQ-FLT-5** Filters apply to **everything displayed**: live video, the test
  pattern, and video memory.

### Video memory (software-supplied image)
- **REQ-VMEM-1** Software writes an RGB888 720p image into DDR and selects it as the
  displayed source (instead of the live input ring).
- **REQ-VMEM-2** Double-buffered: software fills the hidden buffer, writes a "flip"
  register, and the swap happens at the next frame boundary (no tearing).
- **REQ-VMEM-3** Video memory is read by the same MM2S channel as live video and so
  goes through the filter switch and the timestamp overlay.

### Output
- **REQ-OUT-1** Free-running output timing: 1280x720@60 from a local clock, independent
  of the input.
- **REQ-OUT-2** The output keeps running (monitor stays locked) in all cases.
- **REQ-OUT-3** Software selects what is displayed: live video (input ring) or video
  memory. The test pattern is chosen on the input side (REQ-IN-4).

### Timestamp
- **REQ-TS-1** A hardware clock in the fabric holds date and time. Software sets it
  (from the Linux clock); the fabric then keeps counting by itself.
- **REQ-TS-2** The display text is `YYYY-MM-DD HH:MM:SS` (19 characters), drawn with a
  built-in font in the bottom corner. **[ASSUMED]** bottom-right, 8x16 font at an
  adjustable integer scale (default 2x: about 304x32 px).
- **REQ-TS-3** **[ASSUMED]** The overlay sits after the filter switch, so the
  timestamp is always readable (never filtered) and appears over every source. It has an
  enable bit.
- **REQ-TS-4** Setting the time is atomic (shadow registers plus a load strobe). Software
  can read the time back so it can measure drift against the Linux clock and resync.
- **REQ-TS-5** The fabric clock does the calendar rollover itself, including month
  lengths and leap years.
- **REQ-TS-6** After reset or reconfiguration, and until software sets the time, the
  clock reads **2026-01-01 00:00:00** and counts from there.

## 4. Software and interface rules

- **REQ-SW-1** v1 is driven from Jupyter notebooks through the PYNQ overlay, with
  Python drivers in `src/drivers`.
- **REQ-SW-2** **Nothing may be reachable only through PYNQ.** A Rust program (planned,
  much later) must be able to drive the whole pipeline without PYNQ. So:
  - every block has a documented AXI-Lite register map (`docs/register_map.md`, to write);
  - frame buffers sit at **fixed, reserved physical addresses** (device-tree
    reserved-memory), not wherever PYNQ's allocator happens to place them;
  - interrupts are reachable as ordinary Linux UIO devices;
  - the Python drivers are thin wrappers over those registers and nothing more.

## 5. Memory and bandwidth budget

| Item | Value |
|---|---|
| One frame, RGB888 | 1280 x 720 x 3 = 2,764,800 B (2.64 MiB) |
| One stream at 60 fps | about 166 MB/s |
| Input ring (3) + video memory (2) + snapshot (1) | 6 frames = 16,588,800 B (15.8 MiB). Reserve 16 MiB at minimum, 32 MiB with headroom. |
| Steady DDR traffic | S2MM write + one MM2S read, about 332 MB/s, against roughly 1.2 GB/s per HP port |
| Snapshot | one 2.6 MB burst per trigger, negligible on average |

Bandwidth is comfortable. No compression or reduced colour format is needed.

## 6. Clocks

| Clock | Frequency | Use |
|---|---|---|
| Pixel (input) | 74.25 MHz, **switchable** between the recovered HDMI clock and the local clock | input mux, snapshot, VDMA S2MM |
| Pixel (output) | 74.25 MHz, local | VDMA MM2S, filters, timestamp, output |
| Serial (output) | 371.25 MHz (5x) | rgb2dvi |
| IDELAY reference | 200 MHz | HDMI RX |
| AXI / control | about 100 MHz | AXI-Lite, DMA |

The input clock switch must work when the recovered HDMI clock **stops** (cable pulled).
A plain clock mux can hang waiting for the dead clock, so this needs a BUFGCTRL with the
IGNORE inputs, or equivalent. The hardware time counter ticks from the local output
clock, so its drift is set by the oscillator behind that clock.

## 7. Build and repo rules

- `workspace/` is not tracked, so **everything must be reproducible from scripts in git**:
  Vivado Tcl for the block design and constraints, Vitis HLS Tcl or config for each
  filter, and a top-level Makefile or script that builds the bitstream and `.hwh`.
- Sources: HLS and custom cores in `src/cores`, drivers in `src/drivers`.
  **[ASSUMED]** a new `src/overlay` (block-design Tcl, constraints, build scripts) and a
  place for notebooks. Not created yet.

## 8. Verification plan

| Level | What | Covers |
|---|---|---|
| HLS C-sim + co-sim | Each filter against a golden C++ model using sample images | REQ-FLT-* |
| On-board test patterns | The in-fabric test pattern (REQ-IN-4) in place of the HDMI source, through the whole pipeline | REQ-IN, REQ-SNAP, REQ-BUF, REQ-FLT, REQ-OUT |
| Notebook tests on hardware | Jupyter notebooks that exercise each block and compare results (e.g. snapshot vs the known pattern) | all |

## 9. Open questions

- **O-3** HDMI IP: the usual choice is Digilent's `dvi2rgb` / `rgb2dvi`. Needs to be
  confirmed against the PYNQ-Z2 pinout and licence, and imported into the project.
- **O-6** Interrupt plumbing: AXI interrupt controller with a UIO node, so that PYNQ and
  Rust see the same interrupt.
- **O-7** Do the input ring and video memory share one VDMA, with MM2S pointed at
  different frame stores? The flip at a frame boundary (REQ-VMEM-2) depends on how the
  VDMA applies a pointer change, which has to be checked in simulation and on the board.
  If it can't do it cleanly, use a separate frame-buffer reader for video memory.
- **O-8** Speed and shape of the moving element in the test pattern, and whether it
  should be deterministic per frame number so tests can compare snapshots against it.
- **O-9** The return-to-HDMI delay in REQ-IN-5 (1 second assumed).
