"""vidcap - drivers for the PYNQ-Z2 HDMI capture / filter / display overlay."""
from . import lut, regs
from .backend import DevMemMmio, FakeMmio, Mmio, PynqMmio
from .blocks import (ColorLut, Filter, InputSelect, Rtc, Snapshot, StreamMux, TemporalAverage, TimestampOverlay, Vdma,
                     VidcapError)
from .memory import FrameBuffer, HeapAllocator, PynqAllocator, ReservedRegion
from .pipeline import Vidcap
# the interactive panel is in vidcap.ui (imports ipywidgets only when used)

__version__ = "1.2.0"
__all__ = ["Vidcap", "regs", "lut", "Mmio", "PynqMmio", "DevMemMmio", "FakeMmio", "Rtc", "InputSelect",
           "StreamMux", "Filter", "TimestampOverlay", "Snapshot", "Vdma", "VidcapError", "TemporalAverage", "ColorLut",
           "FrameBuffer", "PynqAllocator", "ReservedRegion", "HeapAllocator"]
