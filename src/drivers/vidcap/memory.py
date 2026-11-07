"""Frame memory: buffers the hardware reads and writes by physical address."""
from __future__ import annotations

import mmap
import os
from dataclasses import dataclass
from typing import List

import numpy as np

from .regs import FRAME_BYTES, HEIGHT, WIDTH


@dataclass
class FrameBuffer:
    """A frame the hardware can reach: `phys` is what gets programmed into the DMA engines,
    `array` is the CPU's view as (HEIGHT, WIDTH, 3) uint8 in memory byte order B, G, R."""
    phys: int
    array: np.ndarray
    _keepalive: object = None

    def free(self) -> None:
        if self._keepalive is not None and hasattr(self._keepalive, "freebuffer"):
            self._keepalive.freebuffer()
        self._keepalive = None


class PynqAllocator:
    """Contiguous buffers from PYNQ's CMA allocator (the default on the board). Addresses differ
    from run to run; the driver always reads them back from the buffer object."""

    def alloc(self) -> FrameBuffer:
        from pynq import allocate
        buf = allocate(shape=(HEIGHT, WIDTH, 3), dtype=np.uint8)
        return FrameBuffer(int(buf.physical_address), buf, buf)


class ReservedRegion:
    """Buffers at FIXED physical addresses inside a region the kernel does not use.

    Reserve the memory first (for example a device-tree reserved-memory node, or booting with
    mem= below the top of RAM), then pass its base and size here. This is the route a non-PYNQ
    client takes, because the addresses never change between runs.
    """

    def __init__(self, base: int, size: int):
        self.base, self.size, self._next = base, size, 0
        page = mmap.PAGESIZE
        if base % page:
            raise ValueError("reserved region must be page aligned")
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            self._map = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        finally:
            os.close(fd)

    def alloc(self) -> FrameBuffer:
        step = (FRAME_BYTES + 0xFFF) & ~0xFFF
        if self._next + step > self.size:
            raise MemoryError("reserved region is full")
        off, self._next = self._next, self._next + step
        arr = np.frombuffer(self._map, dtype=np.uint8, count=FRAME_BYTES, offset=off).reshape(HEIGHT, WIDTH, 3)
        return FrameBuffer(self.base + off, arr)


class HeapAllocator:
    """Ordinary numpy arrays with made-up physical addresses. For tests and dry runs only."""

    def __init__(self, base: int = 0x1000_0000):
        self._next = base
        self.allocated: List[FrameBuffer] = []

    def alloc(self) -> FrameBuffer:
        fb = FrameBuffer(self._next, np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8))
        self._next += (FRAME_BYTES + 0xFFF) & ~0xFFF
        self.allocated.append(fb)
        return fb
