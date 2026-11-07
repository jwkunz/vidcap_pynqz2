"""Register maps and base addresses of the vidcap overlay (see docs/register_map.md).

Everything in this module is plain data: offsets are byte offsets inside each block's
AXI-Lite window. Nothing here depends on PYNQ, so another client (for example a Rust
program using /dev/mem or UIO) can take its constants from the same documented map.
"""
from __future__ import annotations

# Base addresses assigned in the block design (src/overlay/vidcap_bd.tcl), 64 KiB each.
BASES = {
    "vdma_live": 0x4000_0000,   # AXI VDMA: live ring (S2MM write + MM2S read)
    "vdma_vmem": 0x4001_0000,   # AXI VDMA: video memory (MM2S read only)
    "rtc":       0x4002_0000,   # vc_rtc            date/time clock
    "isel":      0x4003_0000,   # vc_input_sel      HDMI / test pattern select
    "vmux":      0x4004_0000,   # vc_stream_mux     live / video memory select
    "filt":      0x4005_0000,   # vc_filter         (HLS)
    "ovl":       0x4006_0000,   # vc_overlay        (HLS)
    "snap":      0x4007_0000,   # vc_snapshot       (HLS)
    "intc":      0x4008_0000,   # AXI interrupt controller
    "tavg":      0x4009_0000,   # vc_tavg           temporal average over the last N frames (HLS)
    "lut":       0x400A_0000,   # vc_lut3d          colour grading look-up table (RTL)
}
WINDOW = 0x1_0000

from dataclasses import dataclass


@dataclass(frozen=True)
class Geometry:
    """One video mode. Each mode is its own bitstream, sized for its frame (src/overlay/modes/<name>.env)."""
    name: str
    width: int
    height: int
    pixel_mhz: float

    @property
    def line_bytes(self) -> int:           # 3 bytes per pixel, memory byte order B, G, R
        return self.width * 3

    @property
    def frame_bytes(self) -> int:
        return self.line_bytes * self.height

    @property
    def luma_frame_bytes(self) -> int:     # one brightness frame of the temporal average's history
        return self.width * self.height

    @property
    def sum_frame_bytes(self) -> int:      # its 16-bit running-sum frame
        return self.width * self.height * 2

    @property
    def bitfile(self) -> str:
        return f"vidcap_{self.name}.bit"

    @property
    def shape(self):
        return (self.height, self.width, 3)


MODES = {
    "720p": Geometry("720p", 1280, 720, 74.25),     # CEA VIC 4
    "480p": Geometry("480p", 720, 480, 27.0),       # CEA VIC 2/3
    "240p": Geometry("240p", 1440, 240, 27.0),      # CEA VIC 8/9: 720 real pixels per line, each sent twice
}
DEFAULT_MODE = "720p"

# The default (720p) geometry as plain constants, for code that does not care about the mode.
WIDTH, HEIGHT, BPP = MODES[DEFAULT_MODE].width, MODES[DEFAULT_MODE].height, 3
LINE_BYTES = WIDTH * BPP
FRAME_BYTES = LINE_BYTES * HEIGHT

# Clock of every stream core, register block and VDMA (PS FCLK0).
ACLK_HZ = 100_000_000

# ---- vc_rtc (RTL) ----------------------------------------------------------------------------
RTC_ID, RTC_CTRL, RTC_STATUS, RTC_DATE, RTC_TIME, RTC_CPS, RTC_SUBSEC = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14, 0x18
RTC_MAGIC = 0x5254_4301
RTC_CTRL_LOAD, RTC_CTRL_LATCH, RTC_CTRL_CLR_ERR = 1 << 0, 1 << 1, 1 << 2
RTC_STATUS_VALID, RTC_STATUS_ERR = 1 << 0, 1 << 1

# ---- vc_input_sel (RTL) ----------------------------------------------------------------------
ISEL_ID, ISEL_CTRL, ISEL_STATUS, ISEL_TIMEOUT, ISEL_RETURN = 0x00, 0x04, 0x08, 0x0C, 0x10
ISEL_MEASURED, ISEL_HDMI_FRAMES, ISEL_OUT_FRAMES, ISEL_TPG_PERIOD = 0x14, 0x18, 0x1C, 0x20
ISEL_MAGIC = 0x4953_4C01
ISEL_CTRL_SRC_TPG, ISEL_CTRL_FALLBACK_EN = 1 << 0, 1 << 1
ISEL_ST_LOCKED, ISEL_ST_GOOD, ISEL_ST_FALLBACK, ISEL_ST_ACTIVE_TPG, ISEL_ST_ABORT = (1 << i for i in range(5))

# ---- vc_stream_mux (RTL) ---------------------------------------------------------------------
VMUX_ID, VMUX_SEL, VMUX_STATUS, VMUX_FRAMES = 0x00, 0x04, 0x08, 0x0C
VMUX_MAGIC = 0x4D55_5801

# ---- vc_filter (HLS, offsets from the generated xvc_filter_hw.h) -----------------------------
FILT_MODE, FILT_THRESHOLD, FILT_NOISE = 0x10, 0x18, 0x20
FILTER_MODES = {"passthrough": 0, "invert": 1, "grayscale": 2, "threshold": 3,
                "sobel": 4, "blur": 5, "sharpen": 6, "noise": 7}

# ---- vc_overlay (HLS, from xvc_overlay_hw.h) -------------------------------------------------
OVL_ENABLE, OVL_SCALE, OVL_MARGIN, OVL_FG, OVL_BG, OVL_BG_EN = 0x10, 0x18, 0x20, 0x28, 0x30, 0x38

# ---- vc_snapshot (HLS, from xvc_snapshot_hw.h) -----------------------------------------------
SNAP_MEM_LO, SNAP_MEM_HI, SNAP_TRIGGER, SNAP_ACK, SNAP_STATUS = 0x10, 0x14, 0x1C, 0x24, 0x2C

# ---- vc_tavg (HLS, from xvc_tavg_hw.h) --------------------------------------------------------
TAVG_MEM_R_LO, TAVG_MEM_R_HI = 0x10, 0x14          # buffer base address, read port (64-bit)
TAVG_MEM_W_LO, TAVG_MEM_W_HI = 0x1C, 0x20          # buffer base address, write port: same value as the read port
TAVG_ENABLE, TAVG_N, TAVG_SLOTS, TAVG_RESTART, TAVG_STATUS = 0x28, 0x30, 0x38, 0x40, 0x48
TAVG_MAX_N = 255
TAVG_LUMA_FRAME_BYTES = WIDTH * HEIGHT             # one history frame: 8-bit brightness
TAVG_SUM_FRAME_BYTES = WIDTH * HEIGHT * 2          # the running-sum frame: 16 bits per pixel

# ---- vc_lut3d (RTL): 17x17x17 colour cube, trilinear interpolation -----------------------------
LUT_ID, LUT_CTRL, LUT_STRENGTH, LUT_FRAMES, LUT_WRITES, LUT_CHECKSUM = 0x00, 0x04, 0x08, 0x0C, 0x10, 0x14
LUT_PROBE_IN, LUT_PROBE_OUT = 0x18, 0x1C
LUT_MAGIC = 0x4C55_5403
LUT_CTRL_ENABLE, LUT_CTRL_CLEAR = 1 << 0, 1 << 1
LUT_CUBE = 0x8000                       # write-only window: 8 banks x 4 KiB, see vidcap.lut.to_bank_words()
LUT_STRENGTH_MAX = 256

# ---- AXI VDMA (Xilinx PG020) -----------------------------------------------------------------
VDMA_MM2S_CR, VDMA_MM2S_SR = 0x00, 0x04
VDMA_PARK_PTR, VDMA_VERSION = 0x28, 0x2C
VDMA_S2MM_CR, VDMA_S2MM_SR = 0x30, 0x34
VDMA_MM2S_VSIZE, VDMA_MM2S_HSIZE, VDMA_MM2S_STRIDE, VDMA_MM2S_ADDR0 = 0x50, 0x54, 0x58, 0x5C
VDMA_S2MM_VSIZE, VDMA_S2MM_HSIZE, VDMA_S2MM_STRIDE, VDMA_S2MM_ADDR0 = 0xA0, 0xA4, 0xA8, 0xAC
VDMA_CR_RS, VDMA_CR_CIRCULAR, VDMA_CR_RESET, VDMA_CR_GENLOCK_EN, VDMA_CR_GENLOCK_SRC = 1, 1 << 1, 1 << 2, 1 << 3, 1 << 7
VDMA_SR_HALTED = 1 << 0
VDMA_SR_ERRORS = {4: "internal error", 5: "slave error", 6: "decode error",
                  7: "SOF early", 8: "EOL early", 11: "SOF late", 15: "EOL late"}
