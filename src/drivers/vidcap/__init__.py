"""vidcap - drivers for the PYNQ-Z2 HDMI capture / filter / display overlay."""
from . import regs
from .backend import DevMemMmio, FakeMmio, Mmio, PynqMmio
from .blocks import (Filter, InputSelect, Rtc, Snapshot, StreamMux, TemporalAverage, TimestampOverlay, Vdma,
                     VidcapError)
from .memory import FrameBuffer, HeapAllocator, PynqAllocator, ReservedRegion
from .pipeline import Vidcap
# the interactive panel is in vidcap.ui (imports ipywidgets only when used)

__version__ = "1.1.0"
__all__ = ["Vidcap", "regs", "Mmio", "PynqMmio", "DevMemMmio", "FakeMmio", "Rtc", "InputSelect",
           "StreamMux", "Filter", "TimestampOverlay", "Snapshot", "Vdma", "VidcapError", "TemporalAverage",
           "FrameBuffer", "PynqAllocator", "ReservedRegion", "HeapAllocator"]
