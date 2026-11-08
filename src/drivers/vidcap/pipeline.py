"""High-level control of the whole vidcap pipeline."""
from __future__ import annotations

import os
import time
import warnings
from datetime import datetime
from typing import Callable, Dict, Optional

import numpy as np

from . import regs as R
from .backend import Mmio
from .blocks import (ColorLut, Filter, InputSelect, Rtc, Snapshot, StreamMux, TemporalAverage, TimestampOverlay, Vdma,
                     VidcapError)
from .memory import FrameBuffer

N_LIVE_FRAMES = 3      # live ring, written by the S2MM channel and read by the MM2S channel
N_VMEM_FRAMES = 2      # video memory, double-buffered
FRAME_TIME_S = 1 / 60  # one output frame


def default_history_frames(geo: R.Geometry) -> int:
    """Frames of temporal-average history reserved by default: 16, or 8 when a frame is larger than 3 MB.

    The pool of contiguous memory is 128 MB, of which about 68 MB is free after boot, and every bitstream load leaks a
    few MB. At 1080p the 6 video buffers alone take 37 MB, so 16 frames of history (37 MB more) would not fit.
    """
    return 16 if geo.frame_bytes <= 3_000_000 else 8


class Vidcap:
    """HDMI in -> filter -> timestamp -> HDMI out, plus snapshot and video-memory display.

    Arrays exchanged with this class are (height, width, 3) uint8 for the video mode (720p: 720x1280, 480p: 480x720,
    240p: 240x1440; see `geo`) in memory byte order B, G, R (the order OpenCV uses). Pass rgb=True where offered to
    use R, G, B instead. Each video mode is a separate bitstream: the mode of this object must match the one loaded.
    """

    def __init__(self, mmio_for: Callable[[int], Mmio], allocator, bases: Optional[Dict[str, int]] = None,
                 mode: str = R.DEFAULT_MODE):
        if mode not in R.MODES:
            raise ValueError(f"unknown video mode {mode!r}; choose one of {', '.join(R.MODES)}")
        self.geo = R.MODES[mode]
        self.bases = dict(bases or R.BASES)
        mm = {name: mmio_for(base) for name, base in self.bases.items()}
        self.rtc = Rtc(mm["rtc"])
        self.input = InputSelect(mm["isel"])
        self.mux = StreamMux(mm["vmux"])
        self.filter = Filter(mm["filt"])
        self.overlay = TimestampOverlay(mm["ovl"])
        self.snap = Snapshot(mm["snap"])
        self.tavg = TemporalAverage(mm["tavg"])
        self.lut = ColorLut(mm["lut"])
        self.has_lut = True                  # False for a bitstream built before the colour grading existed
        self.look: Optional[str] = None      # name of the look that is loaded (None = none)
        self.vdma_live = Vdma(mm["vdma_live"])
        self.vdma_vmem = Vdma(mm["vdma_vmem"])
        self._alloc = allocator
        self._live = self._vmem = self._snapbuf = self._tavgbuf = None
        self._front = 0
        self.history_frames = 0              # frames of history the temporal average can use (set by start())
        self.tz: Optional[str] = None            # IANA zone name for the on-screen time; None = this board's own zone

    # ------------------------------------------------------------------ construction
    @classmethod
    def on_pynq(cls, bitfile: Optional[str] = None, download: bool = True, allocator=None,
                protect: bool = True, mode: str = R.DEFAULT_MODE) -> "Vidcap":
        """Load the bitstream for a video mode through PYNQ and return a ready (not yet started) pipeline.

        mode is "720p", "480p" or "240p"; the bitstream vidcap_<mode>.bit is looked for next to the vidcap package
        and in the current folder, unless `bitfile` names one. Switching modes means loading another bitstream
        (about 14 s), so call this again with the other mode.

        Only ONE process (one notebook kernel) should drive the hardware. If another process has the design
        loaded, its DMA engines are stopped first (protect=True) so that replacing the bitstream cannot wedge
        the board's memory port; that other process's Vidcap object is then stale and must be restarted.
        """
        from .backend import PynqMmio
        from .memory import PynqAllocator
        if mode not in R.MODES:
            raise ValueError(f"unknown video mode {mode!r}; choose one of {', '.join(R.MODES)}")
        bitfile = bitfile or cls.find_bitfile(R.MODES[mode])
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
        v = cls(lambda base: PynqMmio(base, R.WINDOW), allocator or PynqAllocator(), mode=mode)
        from . import safety
        v.has_lut = safety.loaded_design_has("lut")      # an older bitstream has no colour grading: never touch its registers
        return v

    @staticmethod
    def find_bitfile(geo: R.Geometry) -> str:
        """Path of vidcap_<mode>.bit: next to the vidcap package (the layout `make deploy` creates), or in the
        current folder."""
        here = os.path.dirname(os.path.abspath(__file__))
        for folder in (os.path.dirname(here), os.getcwd()):
            path = os.path.join(folder, geo.bitfile)
            if os.path.exists(path):
                return path
        raise VidcapError(f"{geo.bitfile} not found next to the vidcap package or in the current folder: "
                          f"is the {geo.name} bitstream built and deployed?")

    # ------------------------------------------------------------------ start / stop
    def check(self) -> None:
        """Verify every block answers with its ID. Fails early if the wrong bitstream is loaded."""
        self.rtc.check(); self.input.check(); self.mux.check()
        if self.has_lut:
            self.lut.check()

    def start(self, source: str = "hdmi", fallback: bool = True, sync_time: bool = True,
              timestamp: bool = True, tz: Optional[str] = None, history_frames: Optional[int] = None) -> None:
        """tz: show the time in this IANA zone (for example "America/New_York"); default is the board's zone.
        history_frames: how many frames of brightness history to reserve for the temporal average, which can then
        average over up to that many frames. Costs one byte per pixel per frame plus two for the sum frame
        (720p: 0.92 MB per frame plus 1.8 MB), taken from the board's contiguous-memory pool (128 MB, shared with
        the video buffers)."""
        if tz is not None:
            self.tz = tz
        self.check()
        a = self._alloc
        self._live = [a.alloc(self.geo) for _ in range(N_LIVE_FRAMES)]
        self._vmem = [a.alloc(self.geo) for _ in range(N_VMEM_FRAMES)]
        self._snapbuf = a.alloc(self.geo)
        for fb in self._live + self._vmem + [self._snapbuf]:
            fb.array[:] = 0
        if history_frames is None:
            history_frames = default_history_frames(self.geo)
        self.history_frames = int(history_frames)
        self._tavgbuf = a.alloc_bytes(TemporalAverage.bytes_needed(self.history_frames, self.geo))
        self._tavgbuf.array[:] = 0

        # control blocks first, so video is in a known state before it starts flowing
        self.mux.select("live", wait=False)
        self.filter.set_mode("passthrough", threshold=128)
        self.overlay.configure(enable=timestamp)
        self.input.set_timeouts(5.0, 1.0)
        self.input.set_fallback(fallback)
        if self.geo.fps != 60.0:
            self.input.set_test_pattern_fps(self.geo.fps)          # the pattern is paced at the mode's own frame rate
        self.input.set_source(source)
        self.snap.set_buffer(self._snapbuf.phys)
        self.tavg.enable(False)                      # off until asked for; the buffer is set first
        self.tavg.set_buffer(self._tavgbuf.phys, self.history_frames)
        self.tavg.set_n(min(8, self.history_frames))
        if sync_time:
            self.sync_time()

        # DMA engines: writer first, then the reader that follows it
        g = self.geo
        size = dict(hsize=g.line_bytes, vsize=g.height, stride=g.line_bytes)
        self.vdma_live.start_s2mm([f.phys for f in self._live], **size)
        self.vdma_live.start_mm2s([f.phys for f in self._live], genlock_slave=True, **size)
        self.vdma_vmem.start_mm2s([f.phys for f in self._vmem], park=0, **size)

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

    # ---- colour grading
    @staticmethod
    def looks() -> Dict[str, str]:
        """The built-in looks: name -> description. Any .cube file works too (set_look)."""
        from . import lut
        return {n: lut.look_description(n) for n in ["identity"] + lut.look_names()}

    def set_look(self, look, strength: float = 100) -> None:
        """Grade the picture with a colour look-up table.

        look: a built-in name (see looks()), a path to a .cube file, the bytes of one, or a (17,17,17,3) uint8 cube
        (vidcap.lut). None or "off" switches grading off. strength is a percentage: 100 = the full look, 0 = none.
        The table is loaded while grading is off, so the picture never shows a half-written table.
        """
        from . import lut
        if not self.has_lut:
            raise VidcapError("this bitstream has no colour grading: load a v1.2.0 or later bitstream")
        if look is None or (isinstance(look, str) and look == "off"):
            self.lut.enable(False)
            self.look = None
            return
        cube = lut.resolve(look)
        if self.lut.enabled():
            self.lut.enable(False)
            time.sleep(3 * FRAME_TIME_S)             # it takes effect on a frame boundary
        self.lut.load(cube)
        self.lut.set_strength(self._strength_code(strength))
        self.lut.enable(True)
        self.look = look if isinstance(look, str) and look in self.looks() else "custom"

    def set_look_strength(self, strength: float) -> None:
        """How much of the look to apply, in percent (0..100), without reloading the table."""
        if not self.has_lut:
            raise VidcapError("this bitstream has no colour grading")
        self.lut.set_strength(self._strength_code(strength))

    @staticmethod
    def _strength_code(percent: float) -> int:
        if not 0 <= percent <= 100:
            raise ValueError("strength is a percentage, 0..100")
        return int(round(percent * R.LUT_STRENGTH_MAX / 100))

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
        """Display an image of this mode's size instead of live video (double-buffered, no tearing)."""
        if image.shape != self.geo.shape or image.dtype != np.uint8:
            raise ValueError(f"image must be uint8 with shape {self.geo.shape} for {self.geo.name}, "
                             f"got {image.dtype} {image.shape}")
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
            "mode": self.geo.name,
            "input": self.input.status(),
            "source_selected": self.input.source(),
            "display": self.mux.active(),
            "filter": self.filter.mode(),
            "temporal_average": {"enabled": self.tavg.enabled(), "n": self.tavg.n(), **self.tavg.status()},
            **({"color_grade": {"look": self.look, "enabled": self.lut.enabled(),
                                "strength_percent": round(self.lut.strength() * 100 / R.LUT_STRENGTH_MAX)}}
               if self.has_lut else {}),
            "timestamp": self.overlay.enabled(),
            "clock": self._clock_text(),
            "clock_was_set": self.rtc.was_set,
            "vdma_live_s2mm": self.vdma_live.status("s2mm"),
            "vdma_live_mm2s": self.vdma_live.status("mm2s"),
            "vdma_vmem_mm2s": self.vdma_vmem.status("mm2s"),
        }
