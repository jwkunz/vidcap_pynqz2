"""High-level control of the whole vidcap pipeline."""
from __future__ import annotations

import time
import warnings
from datetime import datetime
from typing import Callable, Dict, Optional

import numpy as np

from . import regs as R
from .backend import Mmio
from .blocks import (Filter, InputSelect, Rtc, Snapshot, StreamMux, TemporalAverage, TimestampOverlay, Vdma,
                     VidcapError)
from .memory import FrameBuffer

N_LIVE_FRAMES = 3      # live ring, written by the S2MM channel and read by the MM2S channel
N_VMEM_FRAMES = 2      # video memory, double-buffered
FRAME_TIME_S = 1 / 60  # one output frame


class Vidcap:
    """HDMI in -> filter -> timestamp -> HDMI out, plus snapshot and video-memory display.

    Arrays exchanged with this class are (720, 1280, 3) uint8 in memory byte order B, G, R (the
    order OpenCV uses). Pass rgb=True where offered to use R, G, B instead.
    """

    def __init__(self, mmio_for: Callable[[int], Mmio], allocator, bases: Optional[Dict[str, int]] = None):
        self.bases = dict(bases or R.BASES)
        mm = {name: mmio_for(base) for name, base in self.bases.items()}
        self.rtc = Rtc(mm["rtc"])
        self.input = InputSelect(mm["isel"])
        self.mux = StreamMux(mm["vmux"])
        self.filter = Filter(mm["filt"])
        self.overlay = TimestampOverlay(mm["ovl"])
        self.snap = Snapshot(mm["snap"])
        self.tavg = TemporalAverage(mm["tavg"])
        self.vdma_live = Vdma(mm["vdma_live"])
        self.vdma_vmem = Vdma(mm["vdma_vmem"])
        self._alloc = allocator
        self._live = self._vmem = self._snapbuf = self._tavgbuf = None
        self._front = 0
        self.history_frames = 0              # frames of history the temporal average can use (set by start())
        self.tz: Optional[str] = None            # IANA zone name for the on-screen time; None = this board's own zone

    # ------------------------------------------------------------------ construction
    @classmethod
    def on_pynq(cls, bitfile: str = "vidcap.bit", download: bool = True, allocator=None,
                protect: bool = True) -> "Vidcap":
        """Load the bitstream through PYNQ and return a ready (not yet started) pipeline.

        Only ONE process (one notebook kernel) should drive the hardware. If another process has the design
        loaded, its DMA engines are stopped first (protect=True) so that replacing the bitstream cannot wedge
        the board's memory port; that other process's Vidcap object is then stale and must be restarted.
        """
        from .backend import PynqMmio
        from .memory import PynqAllocator
        if download:
            if protect:
                from . import safety
                if safety.design_loaded_since_boot(bitfile):
                    actions = safety.stop_running_design(has_tavg=safety.loaded_design_has("tavg"))
                    warnings.warn("vidcap was already loaded (another notebook or script?). Its DMA engines were "
                                  f"stopped ({'; '.join(actions)}); restart that kernel, it no longer controls the "
                                  "hardware.", RuntimeWarning, stacklevel=2)
            from pynq import Overlay
            cls._overlay = Overlay(bitfile)          # keeps the overlay (and its .hwh) alive
        return cls(lambda base: PynqMmio(base, R.WINDOW), allocator or PynqAllocator())

    # ------------------------------------------------------------------ start / stop
    def check(self) -> None:
        """Verify every block answers with its ID. Fails early if the wrong bitstream is loaded."""
        self.rtc.check(); self.input.check(); self.mux.check()

    def start(self, source: str = "hdmi", fallback: bool = True, sync_time: bool = True,
              timestamp: bool = True, tz: Optional[str] = None, history_frames: int = 16) -> None:
        """tz: show the time in this IANA zone (for example "America/New_York"); default is the board's zone.
        history_frames: how many frames of brightness history to reserve for the temporal average, which can then
        average over up to that many frames. Costs 0.92 MB per frame plus 1.8 MB, taken from the board's
        contiguous-memory pool (128 MB, shared with the video buffers)."""
        if tz is not None:
            self.tz = tz
        self.check()
        a = self._alloc
        self._live = [a.alloc() for _ in range(N_LIVE_FRAMES)]
        self._vmem = [a.alloc() for _ in range(N_VMEM_FRAMES)]
        self._snapbuf = a.alloc()
        for fb in self._live + self._vmem + [self._snapbuf]:
            fb.array[:] = 0
        self.history_frames = int(history_frames)
        self._tavgbuf = a.alloc_bytes(TemporalAverage.bytes_needed(self.history_frames))
        self._tavgbuf.array[:] = 0

        # control blocks first, so video is in a known state before it starts flowing
        self.mux.select("live", wait=False)
        self.filter.set_mode("passthrough", threshold=128)
        self.overlay.configure(enable=timestamp)
        self.input.set_timeouts(5.0, 1.0)
        self.input.set_fallback(fallback)
        self.input.set_source(source)
        self.snap.set_buffer(self._snapbuf.phys)
        self.tavg.enable(False)                      # off until asked for; the buffer is set first
        self.tavg.set_buffer(self._tavgbuf.phys, self.history_frames)
        self.tavg.set_n(min(8, self.history_frames))
        if sync_time:
            self.sync_time()

        # DMA engines: writer first, then the reader that follows it
        self.vdma_live.start_s2mm([f.phys for f in self._live])
        self.vdma_live.start_mm2s([f.phys for f in self._live], genlock_slave=True)
        self.vdma_vmem.start_mm2s([f.phys for f in self._vmem], park=0)

        # The video stream is already flowing when the write channel starts, so it begins in the middle
        # of a frame and flags a one-time "internal error / SOF late". Measured on the board: the flags
        # do not come back once cleared. Let a few frames pass, then clear them, so that status() only
        # ever reports errors that happen after start-up.
        time.sleep(4 * FRAME_TIME_S)
        for vdma, ch in ((self.vdma_live, "s2mm"), (self.vdma_live, "mm2s"), (self.vdma_vmem, "mm2s")):
            vdma.clear_errors(ch)

    def stop(self) -> None:
        # The temporal average writes to memory too: switch it off, and let it finish its frame, BEFORE its buffer
        # is given back (it takes effect at the next frame start, so allow two frames).
        self.tavg.enable(False)
        time.sleep(3 * FRAME_TIME_S)
        for v, ch in ((self.vdma_live, "s2mm"), (self.vdma_live, "mm2s"), (self.vdma_vmem, "mm2s")):
            v.reset(ch)
        for fb in ((self._live or []) + (self._vmem or []) + ([self._snapbuf] if self._snapbuf else [])
                   + ([self._tavgbuf] if self._tavgbuf else [])):
            fb.free()
        self._live = self._vmem = self._snapbuf = self._tavgbuf = None

    # ------------------------------------------------------------------ controls
    def set_filter(self, mode: str, threshold: Optional[int] = None, noise: Optional[int] = None) -> None:
        """Spatial filter or noise. The temporal average is separate (set_temporal_average) and can be combined
        with any of these. mode "noise" adds retro-VCR grain of strength `noise` (0..255)."""
        self.filter.set_mode(mode, threshold, noise)

    def set_temporal_average(self, n: Optional[int]) -> None:
        """Average the brightness of the last `n` frames (1..history_frames); None or 0 switches it off.
        The first frames after switching on or changing n average the frames available so far."""
        if not n:
            self.tavg.enable(False)
            return
        if self._tavgbuf is None:
            raise VidcapError("call start() first")
        self.tavg.set_n(int(n))
        self.tavg.enable(True)

    def use_test_pattern(self, on: bool = True) -> None:
        self.input.set_source("test" if on else "hdmi")

    def now(self) -> datetime:
        """Wall-clock time in the zone the on-screen clock is meant to show (naive, to the second)."""
        if self.tz:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                return datetime.now(ZoneInfo(self.tz)).replace(tzinfo=None, microsecond=0)
            except ZoneInfoNotFoundError:
                raise ValueError(f"unknown time zone {self.tz!r} (use an IANA name such as 'America/New_York')") from None
        return datetime.now().replace(microsecond=0)

    def sync_time(self, tz: Optional[str] = None) -> datetime:
        """Set the hardware clock from this board's clock. `tz` (IANA name) changes the zone it shows, and is
        remembered; pass "" to go back to the board's own zone. The hardware clock keeps plain wall time, so
        call this again after a daylight-saving change."""
        if tz is not None:
            self.tz = tz or None
        now = self.now()
        self.rtc.set(now)
        return now

    def clock_drift(self) -> float:
        """Seconds the hardware clock is ahead (+) of the clock it was set from (in the same zone)."""
        return self.rtc.drift_against(self.now())

    def show_live(self) -> None:
        self.mux.select("live")

    def show_image(self, image: np.ndarray, rgb: bool = False) -> None:
        """Display a 1280x720 image instead of live video (double-buffered, no tearing)."""
        if image.shape != (R.HEIGHT, R.WIDTH, 3) or image.dtype != np.uint8:
            raise ValueError(f"image must be uint8 with shape {(R.HEIGHT, R.WIDTH, 3)}, got {image.dtype} {image.shape}")
        if self._vmem is None:
            raise VidcapError("call start() first")
        hidden = 1 - self._front
        self._vmem[hidden].array[:] = image[..., ::-1] if rgb else image
        self.vdma_vmem.park(hidden)
        time.sleep(2 * FRAME_TIME_S)                  # let the park take effect at a frame boundary
        self._front = hidden
        self.mux.select("vmem")

    def snapshot(self, timeout_s: float = 1.0, rgb: bool = False) -> np.ndarray:
        """Capture the next complete frame from the selected input (before filtering)."""
        if self._snapbuf is None:
            raise VidcapError("call start() first")
        want = self.snap.trigger()
        self.snap.wait(want, timeout_s)
        img = self._snapbuf.array.copy()
        return img[..., ::-1].copy() if rgb else img

    # ------------------------------------------------------------------ diagnostics
    def _clock_text(self) -> str:
        try:
            return str(self.rtc.get())
        except ValueError:                         # registers held an impossible date: report, do not crash
            return "unreadable"

    def status(self) -> Dict[str, object]:
        return {
            "input": self.input.status(),
            "source_selected": self.input.source(),
            "display": self.mux.active(),
            "filter": self.filter.mode(),
            "temporal_average": {"enabled": self.tavg.enabled(), "n": self.tavg.n(), **self.tavg.status()},
            "timestamp": self.overlay.enabled(),
            "clock": self._clock_text(),
            "clock_was_set": self.rtc.was_set,
            "vdma_live_s2mm": self.vdma_live.status("s2mm"),
            "vdma_live_mm2s": self.vdma_live.status("mm2s"),
            "vdma_vmem_mm2s": self.vdma_vmem.status("mm2s"),
        }
