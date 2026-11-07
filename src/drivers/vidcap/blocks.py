"""One small class per hardware block. Each is a thin, documented wrapper over its registers."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional, Sequence, Tuple, Union

from . import lut as L
from . import regs as R
from .backend import Mmio


class VidcapError(RuntimeError):
    pass


def _check_id(name: str, mmio: Mmio, offset: int, expected: int) -> None:
    got = mmio.read(offset)
    if got != expected:
        raise VidcapError(f"{name}: unexpected ID 0x{got:08X} (expected 0x{expected:08X}); "
                          "is the vidcap bitstream loaded?")


# =========================================================================================
class Rtc:
    """Hardware date/time clock that feeds the on-screen timestamp (vc_rtc)."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def check(self) -> None:
        _check_id("rtc", self.m, R.RTC_ID, R.RTC_MAGIC)

    @staticmethod
    def encode_date(dt: datetime) -> int:
        """DATE register: [6:0] yy, [14:8] century, [19:16] month, [28:24] day."""
        return (dt.day << 24) | (dt.month << 16) | ((dt.year // 100) << 8) | (dt.year % 100)

    @staticmethod
    def encode_time(dt: datetime) -> int:
        """TIME register: [4:0] hour, [13:8] minute, [21:16] second."""
        return (dt.second << 16) | (dt.minute << 8) | dt.hour

    @staticmethod
    def decode(date: int, time_: int) -> datetime:
        return datetime((date >> 8 & 0x7F) * 100 + (date & 0x7F), date >> 16 & 0xF, date >> 24 & 0x1F,
                        time_ & 0x1F, time_ >> 8 & 0x3F, time_ >> 16 & 0x3F)

    def set(self, dt: datetime) -> None:
        """Load a time. The clock keeps counting from there by itself."""
        self.m.write(R.RTC_CTRL, R.RTC_CTRL_CLR_ERR)
        self.m.write(R.RTC_DATE, self.encode_date(dt))
        self.m.write(R.RTC_TIME, self.encode_time(dt))
        self.m.write(R.RTC_CTRL, R.RTC_CTRL_LOAD)
        if self.m.read(R.RTC_STATUS) & R.RTC_STATUS_ERR:
            raise VidcapError(f"rtc rejected {dt!r} (invalid date or time)")

    def get(self) -> datetime:
        """Read a consistent date+time (both registers come from one latch)."""
        self.m.write(R.RTC_CTRL, R.RTC_CTRL_LATCH)
        return self.decode(self.m.read(R.RTC_DATE), self.m.read(R.RTC_TIME))

    @property
    def was_set(self) -> bool:
        return bool(self.m.read(R.RTC_STATUS) & R.RTC_STATUS_VALID)

    def cycles_per_second(self, value: Optional[int] = None) -> int:
        """Read, or set, the number of aclk cycles per second (drift trim)."""
        if value is not None:
            self.m.write(R.RTC_CPS, int(value))
        return self.m.read(R.RTC_CPS)

    def drift_against(self, reference: Optional[datetime] = None) -> float:
        """Seconds the hardware clock is ahead (+) of `reference` (default: this machine's clock)."""
        ref = reference or datetime.now()
        return (self.get() - ref).total_seconds()


# =========================================================================================
class InputSelect:
    """Chooses HDMI or the built-in test pattern, with automatic fallback (vc_input_sel)."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def check(self) -> None:
        _check_id("input_sel", self.m, R.ISEL_ID, R.ISEL_MAGIC)

    def set_source(self, source: str) -> None:
        if source not in ("hdmi", "test"):
            raise ValueError("source must be 'hdmi' or 'test'")
        ctrl = self.m.read(R.ISEL_CTRL) & ~R.ISEL_CTRL_SRC_TPG
        if source == "test":
            ctrl |= R.ISEL_CTRL_SRC_TPG
        self.m.write(R.ISEL_CTRL, ctrl)

    def source(self) -> str:
        return "test" if self.m.read(R.ISEL_CTRL) & R.ISEL_CTRL_SRC_TPG else "hdmi"

    def set_fallback(self, enabled: bool) -> None:
        ctrl = self.m.read(R.ISEL_CTRL) & ~R.ISEL_CTRL_FALLBACK_EN
        if enabled:
            ctrl |= R.ISEL_CTRL_FALLBACK_EN
        self.m.write(R.ISEL_CTRL, ctrl)

    def set_timeouts(self, fallback_after_s: float = 5.0, return_after_s: float = 1.0) -> None:
        """How long HDMI must be bad before the test pattern takes over, and good before it returns."""
        self.m.write(R.ISEL_TIMEOUT, int(fallback_after_s * R.ACLK_HZ))
        self.m.write(R.ISEL_RETURN, int(return_after_s * R.ACLK_HZ))

    def timeouts(self) -> Tuple[float, float]:
        return (self.m.read(R.ISEL_TIMEOUT) / R.ACLK_HZ, self.m.read(R.ISEL_RETURN) / R.ACLK_HZ)

    def set_test_pattern_fps(self, fps: float) -> None:
        self.m.write(R.ISEL_TPG_PERIOD, int(R.ACLK_HZ / fps))

    def status(self) -> Dict[str, object]:
        st = self.m.read(R.ISEL_STATUS)
        meas = self.m.read(R.ISEL_MEASURED)
        return {
            "hdmi_locked": bool(st & R.ISEL_ST_LOCKED),
            "hdmi_good": bool(st & R.ISEL_ST_GOOD),
            "fallback_active": bool(st & R.ISEL_ST_FALLBACK),
            "active_source": "test" if st & R.ISEL_ST_ACTIVE_TPG else "hdmi",
            "frame_abort": bool(st & R.ISEL_ST_ABORT),
            "hdmi_size": (meas & 0xFFFF, meas >> 16),
            "hdmi_frames": self.m.read(R.ISEL_HDMI_FRAMES),
            "out_frames": self.m.read(R.ISEL_OUT_FRAMES),
        }

    def clear_abort(self) -> None:
        self.m.write(R.ISEL_STATUS, R.ISEL_ST_ABORT)


# =========================================================================================
class StreamMux:
    """Chooses live video or video memory as the source of the filter chain (vc_stream_mux)."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def check(self) -> None:
        _check_id("stream_mux", self.m, R.VMUX_ID, R.VMUX_MAGIC)

    def select(self, source: str, wait_s: float = 0.5, wait: bool = True) -> None:
        """'live' or 'vmem'. The hardware switches at a frame boundary; this waits for it
        (wait=False just records the request, for use before any video is flowing)."""
        if source not in ("live", "vmem"):
            raise ValueError("source must be 'live' or 'vmem'")
        self.m.write(R.VMUX_SEL, 1 if source == "vmem" else 0)
        if not wait:
            return
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if not self.m.read(R.VMUX_STATUS) & 0b10:       # nothing pending
                return
            time.sleep(0.002)
        raise VidcapError("stream mux did not switch (is video flowing from the selected reader?)")

    def active(self) -> str:
        return "vmem" if self.m.read(R.VMUX_STATUS) & 1 else "live"

    def frames(self) -> int:
        return self.m.read(R.VMUX_FRAMES)


# =========================================================================================
class Filter:
    """Video filter (vc_filter). A mode change takes effect on a frame boundary."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def set_mode(self, mode: Union[str, int], threshold: Optional[int] = None, noise: Optional[int] = None) -> None:
        """Select a filter. `threshold` is used by "threshold"; `noise` (0..255) by "noise" - see set_noise()."""
        code = R.FILTER_MODES[mode] if isinstance(mode, str) else int(mode)
        if not 0 <= code <= 7:
            raise ValueError("mode code must be 0..7")
        if threshold is not None:
            if not 0 <= threshold <= 255:
                raise ValueError("threshold must be 0..255")
            self.m.write(R.FILT_THRESHOLD, threshold)
        if noise is not None:
            self.set_noise(noise)
        self.m.write(R.FILT_MODE, code)

    def set_noise(self, amount: int) -> None:
        """Strength of the retro-VCR grain used by mode "noise": 0 = none, 255 = the pixels move by up to +-127.
        All three colour channels move together (grey snow); the pattern changes every frame."""
        if not 0 <= amount <= 255:
            raise ValueError("noise amount must be 0..255")
        self.m.write(R.FILT_NOISE, amount)

    def noise(self) -> int:
        return self.m.read(R.FILT_NOISE) & 0xFF

    def mode(self) -> str:
        code = self.m.read(R.FILT_MODE) & 7
        names = {v: k for k, v in R.FILTER_MODES.items()}
        return names.get(code, "passthrough")

    def threshold(self) -> int:
        return self.m.read(R.FILT_THRESHOLD) & 0xFF


# =========================================================================================
class ColorLut:
    """Colour grading (vc_lut3d): a 17x17x17 look-up table with interpolation, applied to every pixel after the filter.

    ENABLE and STRENGTH take effect at the next frame. `strength` is 0..256 here (256 = the full look); Vidcap.set_look()
    takes a percentage instead. The table is write-only in hardware: load() checks it with a write count and a checksum.
    """

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def check(self) -> None:
        _check_id("vc_lut3d", self.m, R.LUT_ID, R.LUT_MAGIC)

    def enable(self, on: bool = True) -> None:
        self.m.write(R.LUT_CTRL, R.LUT_CTRL_ENABLE if on else 0)

    def enabled(self) -> bool:
        return bool(self.m.read(R.LUT_CTRL) & R.LUT_CTRL_ENABLE)

    def set_strength(self, strength: int) -> None:
        if not 0 <= strength <= R.LUT_STRENGTH_MAX:
            raise ValueError(f"strength must be 0..{R.LUT_STRENGTH_MAX}")
        self.m.write(R.LUT_STRENGTH, int(strength))

    def strength(self) -> int:
        return self.m.read(R.LUT_STRENGTH) & 0x1FF

    def frames(self) -> int:
        return self.m.read(R.LUT_FRAMES)

    def load(self, cube, verify: bool = True) -> None:
        """Write a 17x17x17x3 cube (see vidcap.lut) into the table. The picture changes while it is written, so
        Vidcap.set_look() switches the table off first."""
        cube = L._check_cube(cube)
        words = L.to_bank_words(cube)
        self.m.write(R.LUT_CTRL, R.LUT_CTRL_CLEAR | (R.LUT_CTRL_ENABLE if self.enabled() else 0))
        for i in L.used_slots():
            self.m.write(R.LUT_CUBE + 4 * int(i), int(words[i]))
        if verify:
            n, got, want = self.m.read(R.LUT_WRITES), self.m.read(R.LUT_CHECKSUM), L.checksum(cube)
            if n != L.NODES or got != want:
                raise VidcapError(f"colour table load failed: hardware counted {n} writes (expected {L.NODES}), "
                                  f"checksum 0x{got:08X} (expected 0x{want:08X})")

    def probe(self) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
        """The first pixel of the latest frame as (R,G,B), before and after the table. Meaningful for a still picture
        (video memory), when strength is 256 or you account for it with vidcap.lut.apply_lut()."""
        def rgb(v):
            return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
        return rgb(self.m.read(R.LUT_PROBE_IN)), rgb(self.m.read(R.LUT_PROBE_OUT))


# =========================================================================================
class TemporalAverage:
    """Moving average of brightness over the last N frames, with a running sum (vc_tavg).

    Needs one contiguous buffer in DDR, set with set_buffer(): `slots` brightness frames (the history ring, one
    byte per pixel) followed by one 16-bit-per-pixel sum frame. N can be anything from 1 up to `slots`.
    Changing N, `slots`, or switching it off and on restarts the average: it then averages the frames it has
    (1, 2, 3 ... N), so the first output frame is the input unchanged. Colour is kept from the current frame."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    @staticmethod
    def bytes_needed(slots: int, geo: Optional[R.Geometry] = None) -> int:
        """Size of the buffer for a history of `slots` frames of the given video mode (default 720p)."""
        g = geo or R.MODES[R.DEFAULT_MODE]
        return slots * g.luma_frame_bytes + g.sum_frame_bytes

    def set_buffer(self, phys: int, slots: int) -> None:
        """Point the hardware at its buffer (read and write ports get the same address) and set the ring size."""
        if not 1 <= slots <= R.TAVG_MAX_N:
            raise ValueError(f"slots must be 1..{R.TAVG_MAX_N}")
        for lo, hi in ((R.TAVG_MEM_R_LO, R.TAVG_MEM_R_HI), (R.TAVG_MEM_W_LO, R.TAVG_MEM_W_HI)):
            self.m.write(lo, phys & 0xFFFF_FFFF)
            self.m.write(hi, phys >> 32)
        self.m.write(R.TAVG_SLOTS, slots)

    def slots(self) -> int:
        return self.m.read(R.TAVG_SLOTS) & 0xFF

    def set_n(self, n: int) -> None:
        if not 1 <= n <= R.TAVG_MAX_N:
            raise ValueError(f"N must be 1..{R.TAVG_MAX_N}")
        if n > self.slots():
            raise ValueError(f"N={n} is more than the {self.slots()} frames of history the buffer holds")
        self.m.write(R.TAVG_N, n)

    def n(self) -> int:
        return self.m.read(R.TAVG_N) & 0xFF

    def enable(self, on: bool = True) -> None:
        self.m.write(R.TAVG_ENABLE, 1 if on else 0)

    def enabled(self) -> bool:
        return bool(self.m.read(R.TAVG_ENABLE) & 1)

    def restart(self) -> None:
        """Empty the window; the next frame starts a new average."""
        self.m.write(R.TAVG_RESTART, (self.m.read(R.TAVG_RESTART) + 1) & 0xFF)

    def status(self) -> Dict[str, int]:
        """depth = frames in the average right now (grows 1..N after a restart); frames = frames since the restart."""
        st = self.m.read(R.TAVG_STATUS)
        return {"depth": st & 0xFF, "frames": (st >> 8) & 0xFF}


# =========================================================================================
Rgb = Tuple[int, int, int]


def _rgb_word(c: Rgb) -> int:
    r, g, b = c
    if not all(0 <= v <= 255 for v in c):
        raise ValueError("colour components must be 0..255")
    return (r << 16) | (g << 8) | b


class TimestampOverlay:
    """On-screen date/time (vc_overlay). The time itself comes from the hardware clock."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def configure(self, enable: bool = True, scale: int = 2, margin: int = 16,
                  fg: Rgb = (255, 255, 255), bg: Optional[Rgb] = (0, 0, 0)) -> None:
        """scale 1..4 (text is 19 characters of 8x16 pixels times scale); bg=None for transparent."""
        if not 1 <= scale <= 4:
            raise ValueError("scale must be 1..4")
        if not 0 <= margin <= 255:
            raise ValueError("margin must be 0..255")
        self.m.write(R.OVL_SCALE, scale)
        self.m.write(R.OVL_MARGIN, margin)
        self.m.write(R.OVL_FG, _rgb_word(fg))
        self.m.write(R.OVL_BG, _rgb_word(bg or (0, 0, 0)))
        self.m.write(R.OVL_BG_EN, 0 if bg is None else 1)
        self.m.write(R.OVL_ENABLE, 1 if enable else 0)

    def enable(self, on: bool = True) -> None:
        self.m.write(R.OVL_ENABLE, 1 if on else 0)

    def enabled(self) -> bool:
        return bool(self.m.read(R.OVL_ENABLE) & 1)


# =========================================================================================
class Snapshot:
    """Single-frame capture to DDR (vc_snapshot)."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def set_buffer(self, phys: int) -> None:
        self.m.write(R.SNAP_MEM_LO, phys & 0xFFFF_FFFF)
        self.m.write(R.SNAP_MEM_HI, phys >> 32)

    def _status(self) -> int:
        return self.m.read(R.SNAP_STATUS)

    def done_count(self) -> int:
        return (self._status() >> 8) & 0xFF

    def busy(self) -> bool:
        return bool(self._status() & 1)

    def trigger(self) -> int:
        """Arm a capture of the next frame. Returns the done count to wait for."""
        want = (self.done_count() + 1) & 0xFF
        self.m.write(R.SNAP_TRIGGER, (self.m.read(R.SNAP_TRIGGER) + 1) & 0xFF)
        return want

    def wait(self, want_done: int, timeout_s: float = 1.0) -> None:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self.done_count() == want_done:
                self.ack()
                return
            time.sleep(0.001)
        raise VidcapError("snapshot did not complete (no video frames reaching the snapshot core?)")

    def ack(self) -> None:
        """Tell the hardware how many completions software has handled (drops the interrupt)."""
        self.m.write(R.SNAP_ACK, self.done_count())

    def irq_pending(self) -> bool:
        return self.done_count() != (self.m.read(R.SNAP_ACK) & 0xFF)


# =========================================================================================
class Vdma:
    """Register-level driver for an AXI VDMA (Xilinx PG020). Uses fixed frame-store addresses."""

    def __init__(self, mmio: Mmio):
        self.m = mmio

    def version(self) -> int:
        return self.m.read(R.VDMA_VERSION)

    def reset(self, channel: str) -> None:
        cr = R.VDMA_S2MM_CR if channel == "s2mm" else R.VDMA_MM2S_CR
        self.m.write(cr, R.VDMA_CR_RESET)
        for _ in range(1000):
            if not self.m.read(cr) & R.VDMA_CR_RESET:
                return
            time.sleep(0.0005)
        raise VidcapError(f"vdma {channel} reset did not complete")

    def _program(self, base: int, frames: Sequence[int], hsize: int, vsize: int, stride: int,
                 vsize_reg: int, hsize_reg: int, stride_reg: int, addr0: int) -> None:
        for i, addr in enumerate(frames):
            self.m.write(addr0 + 4 * i, addr)
        self.m.write(stride_reg, stride & 0xFFFF)          # frame delay = 0
        self.m.write(hsize_reg, hsize)
        self.m.write(vsize_reg, vsize)                      # writing VSIZE starts the transfer

    def start_s2mm(self, frames: Sequence[int], hsize: int = R.LINE_BYTES, vsize: int = R.HEIGHT,
                   stride: int = R.LINE_BYTES) -> None:
        """Write channel: circular over `frames`, genlock master (internal source)."""
        self.reset("s2mm")
        self.m.write(R.VDMA_S2MM_CR, R.VDMA_CR_RS | R.VDMA_CR_CIRCULAR | R.VDMA_CR_GENLOCK_SRC)
        self._program(0, frames, hsize, vsize, stride, R.VDMA_S2MM_VSIZE, R.VDMA_S2MM_HSIZE,
                      R.VDMA_S2MM_STRIDE, R.VDMA_S2MM_ADDR0)

    def start_mm2s(self, frames: Sequence[int], hsize: int = R.LINE_BYTES, vsize: int = R.HEIGHT,
                   stride: int = R.LINE_BYTES, genlock_slave: bool = False, park: Optional[int] = None) -> None:
        """Read channel. genlock_slave=True follows the writer (live ring); `park` shows one frame."""
        self.reset("mm2s")
        cr = R.VDMA_CR_RS
        if park is None:
            cr |= R.VDMA_CR_CIRCULAR
        if genlock_slave:
            cr |= R.VDMA_CR_GENLOCK_EN | R.VDMA_CR_GENLOCK_SRC
        self.m.write(R.VDMA_MM2S_CR, cr)
        if park is not None:
            self.park(park)
        self._program(0, frames, hsize, vsize, stride, R.VDMA_MM2S_VSIZE, R.VDMA_MM2S_HSIZE,
                      R.VDMA_MM2S_STRIDE, R.VDMA_MM2S_ADDR0)

    def park(self, frame: int) -> None:
        """Make the read channel show frame `frame` (takes effect at a frame boundary)."""
        cur = self.m.read(R.VDMA_PARK_PTR)
        self.m.write(R.VDMA_PARK_PTR, (cur & ~0x1F) | (frame & 0x1F))

    def status(self, channel: str) -> Dict[str, object]:
        sr = self.m.read(R.VDMA_S2MM_SR if channel == "s2mm" else R.VDMA_MM2S_SR)
        errors = [name for bit, name in R.VDMA_SR_ERRORS.items() if sr & (1 << bit)]
        return {"raw": sr, "halted": bool(sr & R.VDMA_SR_HALTED), "errors": errors}

    def clear_errors(self, channel: str) -> None:
        sr = R.VDMA_S2MM_SR if channel == "s2mm" else R.VDMA_MM2S_SR
        mask = sum(1 << b for b in R.VDMA_SR_ERRORS)
        self.m.write(sr, mask)                              # write-1-to-clear
