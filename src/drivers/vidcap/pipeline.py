"""High-level control of the whole vidcap pipeline."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Dict, Optional

import numpy as np

from . import regs as R
from .backend import Mmio
from .blocks import (Filter, InputSelect, Rtc, Snapshot, StreamMux, TimestampOverlay, Vdma,
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
        self.vdma_live = Vdma(mm["vdma_live"])
        self.vdma_vmem = Vdma(mm["vdma_vmem"])
        self._alloc = allocator
        self._live = self._vmem = self._snapbuf = None
        self._front = 0

    # ------------------------------------------------------------------ construction
    @classmethod
    def on_pynq(cls, bitfile: str = "vidcap.bit", download: bool = True, allocator=None) -> "Vidcap":
        """Load the bitstream through PYNQ and return a ready (not yet started) pipeline."""
        from .backend import PynqMmio
        from .memory import PynqAllocator
        if download:
            from pynq import Overlay
            cls._overlay = Overlay(bitfile)          # keeps the overlay (and its .hwh) alive
        return cls(lambda base: PynqMmio(base, R.WINDOW), allocator or PynqAllocator())

    # ------------------------------------------------------------------ start / stop
    def check(self) -> None:
        """Verify every block answers with its ID. Fails early if the wrong bitstream is loaded."""
        self.rtc.check(); self.input.check(); self.mux.check()

    def start(self, source: str = "hdmi", fallback: bool = True, sync_time: bool = True,
              timestamp: bool = True) -> None:
        self.check()
        a = self._alloc
        self._live = [a.alloc() for _ in range(N_LIVE_FRAMES)]
        self._vmem = [a.alloc() for _ in range(N_VMEM_FRAMES)]
        self._snapbuf = a.alloc()
        for fb in self._live + self._vmem + [self._snapbuf]:
            fb.array[:] = 0

        # control blocks first, so video is in a known state before it starts flowing
        self.mux.select("live", wait=False)
        self.filter.set_mode("passthrough", threshold=128)
        self.overlay.configure(enable=timestamp)
        self.input.set_timeouts(5.0, 1.0)
        self.input.set_fallback(fallback)
        self.input.set_source(source)
        self.snap.set_buffer(self._snapbuf.phys)
        if sync_time:
            self.sync_time()

        # DMA engines: writer first, then the reader that follows it
        self.vdma_live.start_s2mm([f.phys for f in self._live])
        self.vdma_live.start_mm2s([f.phys for f in self._live], genlock_slave=True)
        self.vdma_vmem.start_mm2s([f.phys for f in self._vmem], park=0)

    def stop(self) -> None:
        for v, ch in ((self.vdma_live, "s2mm"), (self.vdma_live, "mm2s"), (self.vdma_vmem, "mm2s")):
            v.reset(ch)
        for fb in (self._live or []) + (self._vmem or []) + ([self._snapbuf] if self._snapbuf else []):
            fb.free()
        self._live = self._vmem = self._snapbuf = None

    # ------------------------------------------------------------------ controls
    def set_filter(self, mode: str, threshold: Optional[int] = None) -> None:
        self.filter.set_mode(mode, threshold)

    def use_test_pattern(self, on: bool = True) -> None:
        self.input.set_source("test" if on else "hdmi")

    def sync_time(self) -> datetime:
        now = datetime.now().replace(microsecond=0)
        self.rtc.set(now)
        return now

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
    def status(self) -> Dict[str, object]:
        return {
            "input": self.input.status(),
            "source_selected": self.input.source(),
            "display": self.mux.active(),
            "filter": self.filter.mode(),
            "timestamp": self.overlay.enabled(),
            "clock": str(self.rtc.get()),
            "clock_was_set": self.rtc.was_set,
            "vdma_live_s2mm": self.vdma_live.status("s2mm"),
            "vdma_live_mm2s": self.vdma_live.status("mm2s"),
            "vdma_vmem_mm2s": self.vdma_vmem.status("mm2s"),
        }
