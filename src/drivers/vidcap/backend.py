"""Register access back-ends. Blocks only ever call read(offset) / write(offset, value)."""
from __future__ import annotations

import mmap
import os
import struct
from typing import Callable, Dict, Optional


class Mmio:
    """One block's AXI-Lite window: 32-bit registers at byte offsets."""

    def read(self, offset: int) -> int:
        raise NotImplementedError

    def write(self, offset: int, value: int) -> None:
        raise NotImplementedError


class PynqMmio(Mmio):
    """Window through PYNQ's MMIO class (the normal route on the board)."""

    def __init__(self, base: int, length: int = 0x1_0000):
        from pynq import MMIO  # imported here so the rest of the package works without PYNQ
        self._m = MMIO(base, length)

    def read(self, offset: int) -> int:
        return int(self._m.read(offset))

    def write(self, offset: int, value: int) -> None:
        self._m.write(offset, value & 0xFFFF_FFFF)


class DevMemMmio(Mmio):
    """Window through /dev/mem. No PYNQ needed; useful for tools and non-Python clients."""

    def __init__(self, base: int, length: int = 0x1_0000):
        page = mmap.PAGESIZE
        self._off = base % page
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            self._map = mmap.mmap(fd, length + self._off, mmap.MAP_SHARED,
                                  mmap.PROT_READ | mmap.PROT_WRITE, offset=base - self._off)
        finally:
            os.close(fd)

    def read(self, offset: int) -> int:
        return struct.unpack_from("<I", self._map, self._off + offset)[0]

    def write(self, offset: int, value: int) -> None:
        struct.pack_into("<I", self._map, self._off + offset, value & 0xFFFF_FFFF)


class FakeMmio(Mmio):
    """Dictionary-backed registers for tests, with a log of every access and optional hooks."""

    def __init__(self, initial: Optional[Dict[int, int]] = None):
        self.regs: Dict[int, int] = dict(initial or {})
        self.log: list = []                                    # ("r"|"w", offset, value)
        self.on_write: Dict[int, Callable[[int, "FakeMmio"], None]] = {}
        self.on_read: Dict[int, Callable[["FakeMmio"], int]] = {}

    def read(self, offset: int) -> int:
        if offset in self.on_read:
            self.regs[offset] = self.on_read[offset](self)
        value = self.regs.get(offset, 0)
        self.log.append(("r", offset, value))
        return value

    def write(self, offset: int, value: int) -> None:
        value &= 0xFFFF_FFFF
        self.log.append(("w", offset, value))
        self.regs[offset] = value
        if offset in self.on_write:
            self.on_write[offset](value, self)

    def writes(self):
        return [(o, v) for k, o, v in self.log if k == "w"]
