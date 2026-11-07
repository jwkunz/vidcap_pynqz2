# vidcap register map

This is the hardware contract. Anything that can read and write physical memory (the Python
drivers under `src/drivers`, a Rust program, a bare-metal app) can drive the whole pipeline from
this page alone; nothing depends on PYNQ.

All registers are 32 bits wide, accessed at 4-byte aligned offsets. Every block is clocked by
`aclk` = 100 MHz (PS `FCLK0`). Addresses are physical, on the PS general-purpose AXI port 0.

| Block | Base address | What it is |
|---|---|---|
| `vdma_live` | `0x4000_0000` | AXI VDMA: live ring. S2MM writes, MM2S reads. 3 frame stores |
| `vdma_vmem` | `0x4001_0000` | AXI VDMA: video memory, MM2S only. 2 frame stores |
| `rtc` | `0x4002_0000` | `vc_rtc` hardware date/time clock |
| `isel` | `0x4003_0000` | `vc_input_sel` HDMI / test-pattern select |
| `vmux` | `0x4004_0000` | `vc_stream_mux` live / video-memory select |
| `filt` | `0x4005_0000` | `vc_filter` video filter (HLS) |
| `ovl` | `0x4006_0000` | `vc_overlay` timestamp overlay (HLS) |
| `snap` | `0x4007_0000` | `vc_snapshot` frame capture (HLS) |
| `intc` | `0x4008_0000` | AXI interrupt controller |
| `tavg` | `0x4009_0000` | `vc_tavg` temporal average over the last N frames (HLS) |

The authoritative source for these addresses is `src/overlay/vidcap_bd.tcl` (the `map` list at the
end); the register offsets below for HLS blocks are checked against the HLS-generated headers by
`src/drivers/tests/test_vidcap.py`.

## Video format

* 1280 x 720 at 60 Hz, 24 bits per pixel.
* **In memory:** 3 bytes per pixel in the order **B, G, R**, rows packed back to back with no
  padding (`stride = 3840` bytes), `frame = 3840 x 720 = 2,764,800` bytes. This is the layout
  OpenCV and PYNQ use for BGR images.
* On the internal AXI4-Stream links `tdata[23:16] = R`, `[15:8] = G`, `[7:0] = B`, `tuser` marks the
  first pixel of a frame and `tlast` the last pixel of a line.
* Frame buffers must be physically contiguous, 4 KiB aligned recommended, below 512 MiB (the board's
  DDR), and **not owned by Linux** while the hardware uses them.

## `rtc` — `vc_rtc`

Powers up at **2026-01-01 00:00:00** and counts by itself. Software sets the real time once.

| Offset | Name | Access | Description |
|---|---|---|---|
| `0x00` | ID | R | `0x52544301` |
| `0x04` | CTRL | W | bit0 **LOAD** copy DATE/TIME into the live clock (rejected if invalid). bit1 **LATCH** capture the live clock into the readable DATE/TIME. bit2 **CLR_ERR** |
| `0x08` | STATUS | R | bit0 VALID (a time has been loaded since reset), bit1 ERR (last LOAD rejected) |
| `0x0C` | DATE | RW | write = shadow, read = latched copy. `[6:0]` yy (0-99), `[14:8]` century (0-99), `[19:16]` month (1-12), `[28:24]` day (1-31) |
| `0x10` | TIME | RW | write = shadow, read = latched copy. `[4:0]` hour (0-23), `[13:8]` minute, `[21:16]` second |
| `0x14` | CPS | RW | aclk cycles per second, default 100,000,000. Trim this to correct drift |
| `0x18` | SUBSEC | R | aclk cycles elapsed in the current second |

Year = century x 100 + yy. To set a time: write DATE, write TIME, write CTRL = 1, read STATUS.
To read: write CTRL = 2, then read DATE and TIME (both come from one latch).

## `isel` — `vc_input_sel`

Selects the video source and guarantees whole 1280 x 720 frames downstream (pads or drops a
malformed HDMI frame; switches only on frame boundaries).

| Offset | Name | Access | Description |
|---|---|---|---|
| `0x00` | ID | R | `0x49534C01` |
| `0x04` | CTRL | RW | bit0 **SRC**: 0 = HDMI, 1 = test pattern. bit1 **FALLBACK_EN** (default 1) |
| `0x08` | STATUS | R/W1C | bit0 HDMI locked, bit1 HDMI good (locked, 1280x720, frames arriving), bit2 fallback active, bit3 test pattern is the active source, bit4 frame-abort (sticky; **write 1 to clear**) |
| `0x0C` | TIMEOUT | RW | aclk cycles of bad HDMI before falling back to the test pattern. Default 500,000,000 (5 s) |
| `0x10` | RETURN | RW | aclk cycles of good HDMI before leaving fallback. Default 100,000,000 (1 s) |
| `0x14` | MEASURED | R | `[15:0]` width, `[31:16]` height of the last HDMI frame |
| `0x18` | HDMI_FRAMES | R | HDMI frames seen |
| `0x1C` | OUT_FRAMES | R | frames emitted |
| `0x20` | TPG_PERIOD | RW | aclk cycles per test-pattern frame. Default 1,666,666 (60 fps) |

Fallback only applies while SRC = HDMI and FALLBACK_EN = 1. The test pattern is 8 colour bars with a
64 x 64 box that moves 8 px right and 4 px down per frame and bounces off the edges.

## `vmux` — `vc_stream_mux`

| Offset | Name | Access | Description |
|---|---|---|---|
| `0x00` | ID | R | `0x4D555801` |
| `0x04` | SEL | RW | 0 = live video (`vdma_live` MM2S), 1 = video memory (`vdma_vmem` MM2S). Takes effect at a frame boundary |
| `0x08` | STATUS | R | bit0 ACTIVE source, bit1 PENDING (SEL differs from ACTIVE) |
| `0x0C` | FRAMES | R | frames forwarded |

SEL only completes if the selected reader is producing frames (its VDMA must be running).

## `filt` — `vc_filter` (HLS, `s_axi_control`)

| Offset | Name | Bits | Description |
|---|---|---|---|
| `0x10` | MODE | `[2:0]` | 0 passthrough (default), 1 invert, 2 grayscale, 3 threshold, 4 sobel, 5 blur, 6 sharpen, 7 noise |
| `0x18` | THRESHOLD | `[7:0]` | luma threshold for mode 3: pixel is white if luma > THRESHOLD |
| `0x20` | NOISE_AMOUNT | `[7:0]` | strength of mode 7: 0 = none, 255 = pixels move by up to +-127 |

Sampled once at the start of each frame. There is no start/stop register: the core free-runs.
Edges are handled by replicating the border pixel. Luma = (77 R + 150 G + 29 B) >> 8. Sobel is
|Gx| + |Gy| of the luma, clamped to 255. Blur is a 3x3 Gaussian (1 2 1 / 2 4 2 / 1 2 1) / 16.
Sharpen is 5 x centre minus the four neighbours, clamped.

**Noise (mode 7)** adds retro-VCR grain. A free-running 32-bit Galois LFSR (taps 32, 22, 2, 1; polynomial `0x80200003`,
seed `0xACE1ACE1` at power-up) is advanced 8 steps per **output pixel in every mode** and never reset. The low byte of the
state is a signed sample *s*; all three channels of the pixel change by `(s x NOISE_AMOUNT) >> 8` (arithmetic shift), clamped
to 0..255, so the noise is grey and differs in every frame.

## `ovl` — `vc_overlay` (HLS, `s_axi_control`)

Draws `YYYY-MM-DD HH:MM:SS` (19 characters, each 8 x 16 pixels times SCALE) in the bottom-right
corner, from the `rtc` digits, sampled once per frame. Registers reset to 0, so the overlay is **off**
until configured.

| Offset | Name | Bits | Description |
|---|---|---|---|
| `0x10` | ENABLE | `[0]` | 1 = draw the text |
| `0x18` | SCALE | `[2:0]` | 1..4 (0 acts as 1, larger as 4) |
| `0x20` | MARGIN | `[7:0]` | pixels from the right and bottom edges |
| `0x28` | FG | `[23:0]` | text colour `{R,G,B}` |
| `0x30` | BG | `[23:0]` | background box colour `{R,G,B}` |
| `0x38` | BG_EN | `[0]` | 1 = draw the background box; 0 = transparent |

If the text box would not fit on screen at the chosen SCALE and MARGIN, nothing is drawn.

## `snap` — `vc_snapshot` (HLS, `s_axi_control`)

Sits in series in the video stream, before the frame buffer and before any filtering. It always passes
video through; when triggered it also writes the next complete frame to DDR.

| Offset | Name | Access | Description |
|---|---|---|---|
| `0x10` | MEM_LO | RW | snapshot buffer physical address, bits 31:0 |
| `0x14` | MEM_HI | RW | bits 63:32 (write 0) |
| `0x1C` | TRIGGER | RW | 8-bit counter. Write (current value + 1) to capture the next frame |
| `0x24` | ACK | RW | 8-bit counter. Write the DONE count you have handled |
| `0x2C` | STATUS | R | bit0 BUSY (capture in progress), `[15:8]` DONE count (captures completed) |

The `irq` output is high while DONE count != ACK. Typical use: read DONE (`d`), write TRIGGER+1, wait
until DONE = `d + 1`, read the buffer, write ACK = DONE.

## `tavg` — `vc_tavg` (HLS, `s_axi_control`)

Moving average of the brightness over the last N frames, between the live/video-memory mux and the filter. It uses a running sum
so the memory traffic does not depend on N:

    S[t] = S[t-1] + Y[t] - Y[t-N]        average[t] = S[t] / N        out = clamp(rgb + (average - Y_now))

`Y` is 8-bit luma `(77 R + 150 G + 29 B) >> 8`; the sum is exact 16-bit integer arithmetic. Colour is kept from the current
frame. While fewer than N frames have been seen since a restart it averages the frames it has, so the first output is the input
unchanged. `average = (S x R + 0x8000) >> 16` with `R = round(65536 / frames_in_window)`, which gives exactly the input
back for a constant picture. Off (the default), it passes video through and does no memory traffic.

| Offset | Name | Access | Description |
|---|---|---|---|
| `0x10` | MEM_R_LO / HI (`0x14`) | RW | base address of the buffer, read port (64-bit) |
| `0x1C` | MEM_W_LO / HI (`0x20`) | RW | base address of the buffer, write port: **write the same address as MEM_R** |
| `0x28` | ENABLE | RW | bit0: 1 = averaging, 0 = pass-through (history is discarded) |
| `0x30` | N | RW | window length 1..255 (clamped to SLOTS) |
| `0x38` | SLOTS | RW | number of brightness frames the buffer holds (ring size), 1..255 |
| `0x40` | RESTART | RW | 8-bit counter: writing a different value empties the window |
| `0x48` | STATUS | R | `[7:0]` frames in the average now, `[15:8]` frames since the last restart |

All controls are sampled once at the start of each frame. The average restarts by itself when ENABLE goes 0 to 1, or N or
SLOTS changes.

**Buffer layout** (one contiguous, physically addressed region, no alignment requirement beyond 8 bytes; 4 KiB recommended):

    [ ring: SLOTS brightness frames, W*H bytes each ][ sum frame: W*H little-endian 16-bit values ]

Size = SLOTS x 921,600 + 1,843,200 bytes. Frame *t* is written to ring slot (*t* mod SLOTS) and the frame that leaves the window
is read from slot (*t* - N) mod SLOTS. The buffer does not need to be cleared. Memory traffic while on: read the oldest frame
(1 byte/pixel), read the sum (2), write the sum (2), write the new frame (1), about 330 MB/s at 720p60, whatever N is.
Switch it off and wait two frames before freeing the buffer.

## `vdma_live`, `vdma_vmem` — AXI VDMA

Standard Xilinx AXI VDMA (PG020); registers used:

| Offset | Name | Notes |
|---|---|---|
| `0x00` / `0x30` | MM2S_VDMACR / S2MM_VDMACR | bit0 RS, bit1 circular (0 = park), bit2 reset, bit3 genlock enable, bit7 genlock source |
| `0x04` / `0x34` | MM2S_VDMASR / S2MM_VDMASR | bit0 halted; error bits 4-8, 11, 15; write 1 to clear |
| `0x28` | PARK_PTR_REG | bits 4:0 read-channel frame to display in park mode |
| `0x50` `0x54` `0x58` | MM2S_VSIZE / HSIZE / FRMDLY_STRIDE | VSIZE = 720, HSIZE = 3840, stride = 3840 |
| `0x5C` ... | MM2S_START_ADDRESS 1..N | frame buffer addresses, 4 bytes each |
| `0xA0` `0xA4` `0xA8` | S2MM_VSIZE / HSIZE / FRMDLY_STRIDE | same values |
| `0xAC` ... | S2MM_START_ADDRESS 1..N | frame buffer addresses |

Start sequence per channel: reset, write control (RS etc.), write the start addresses, stride, HSIZE,
and **VSIZE last** (that write starts the transfer).

* `vdma_live`: 3 frame stores. S2MM is genlock master, circular. MM2S is genlock slave, circular, so the
  reader shows the frame the writer last completed and never one being written.
* `vdma_vmem`: 2 frame stores, MM2S only, **park mode**. Write the hidden buffer, then write PARK_PTR to
  flip. Select it through `vmux`.

## Interrupts

`intc` (AXI INTC) collects four level-high lines and drives PS `IRQ_F2P[0]`:

| Bit | Source |
|---|---|
| 0 | `snap` done (DONE count != ACK) |
| 1 | `vdma_live` S2MM |
| 2 | `vdma_live` MM2S |
| 3 | `vdma_vmem` MM2S |

## Status LEDs

| LED | Meaning |
|---|---|
| LD0 | HDMI input good |
| LD1 | test-pattern fallback active |
| LD2 | test pattern is the active source |
| LD3 | HDMI output pixel clock locked |

## Bring-up sequence for a client that does not use the Python drivers

1. Load `vidcap.bit` (and the matching `vidcap.hwh` if using PYNQ).
2. Check the three ID registers (`rtc`, `isel`, `vmux`).
3. Allocate 6 frames: 3 live, 2 video memory, 1 snapshot. Zero them. Optionally allocate the `tavg` buffer (SLOTS x 921,600 + 1,843,200 bytes).
4. Set `filt` MODE = 0, THRESHOLD; configure `ovl`; set `isel` timeouts, FALLBACK_EN, SRC; write `snap`
   MEM; if using `tavg`, write its buffer address to both MEM_R and MEM_W, then SLOTS and N, with ENABLE = 0; write the real time to `rtc`.
5. Start `vdma_live` S2MM, then `vdma_live` MM2S (genlock slave), then `vdma_vmem` MM2S (parked).
6. To show an image: write it into the hidden `vdma_vmem` frame, write PARK_PTR, set `vmux` SEL = 1.
