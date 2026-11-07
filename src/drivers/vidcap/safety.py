"""Protection against replacing a running design.

Loading a bitstream while a VDMA still has memory transactions in flight can leave the Zynq's memory
port (HP0) waiting for a write that never completes. Every later session then stalls after its first
frame, DMA resets time out, and only a reboot clears it. This happened when a second Jupyter kernel
loaded the overlay while the first kernel's DMA was still running.

So before a bitstream is replaced, the DMA engines of the design that is currently loaded are stopped.
"""
from __future__ import annotations

import os
import time
from typing import Callable, List, Optional

from . import regs as R
from .backend import DevMemMmio, Mmio
from .blocks import VidcapError


def design_loaded_since_boot(bitfile: str, state_file: Optional[str] = None, boot_time: Optional[float] = None,
                             loaded_name: Optional[str] = None) -> bool:
    """True if PYNQ says `bitfile` is the loaded bitstream AND that was recorded after this boot.

    PYNQ keeps the loaded-bitstream name in a file that survives a reboot, but the FPGA does not, so the
    file alone is not proof: touching the registers of an unconfigured FPGA can hang the CPU.
    """
    if loaded_name is None:
        from pynq import PL
        loaded_name = PL.bitfile_name
    if not loaded_name or os.path.basename(str(loaded_name)) != os.path.basename(bitfile):
        return False
    if state_file is None:
        import pynq.pl_server as server
        state_file = os.path.join(os.path.dirname(server.__file__), "global_pl_state_.json")
    if boot_time is None:
        boot_time = time.time() - float(open("/proc/uptime").read().split()[0])
    try:
        return os.path.getmtime(state_file) >= boot_time
    except OSError:
        return False


def _stop_channel(m: Mmio, cr: int, sr: int, timeout_s: float) -> str:
    """Stop one VDMA channel. Returns "idle", "stopped" or "reset"; raises if it will not stop."""
    if m.read(sr) & R.VDMA_SR_HALTED:
        return "idle"
    m.write(cr, m.read(cr) & ~R.VDMA_CR_RS)                      # finish the current frame, then halt
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if m.read(sr) & R.VDMA_SR_HALTED:
            return "stopped"
        time.sleep(0.002)
    m.write(cr, R.VDMA_CR_RESET)                                  # still running: force it
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if m.read(sr) & R.VDMA_SR_HALTED and not m.read(cr) & R.VDMA_CR_RESET:
            return "reset"
        time.sleep(0.002)
    raise VidcapError("a DMA engine of the design that is already loaded will not stop. Replacing the bitstream now "
                      "would wedge the board's memory port. Reboot the board, then load the design.")


def stop_running_design(mmio_for: Callable[[int], Mmio] = DevMemMmio, timeout_s: float = 0.5) -> List[str]:
    """Stop the DMA engines of the currently loaded vidcap design. Returns what was done, for logging."""
    done = []
    live, vmem = mmio_for(R.BASES["vdma_live"]), mmio_for(R.BASES["vdma_vmem"])
    # the snapshot core writes to memory too, but only for a few tens of milliseconds per capture
    snap = mmio_for(R.BASES["snap"])
    end = time.monotonic() + timeout_s
    while snap.read(R.SNAP_STATUS) & 1 and time.monotonic() < end:
        time.sleep(0.005)
    # writers first, so nothing new is stored while the readers wind down
    for name, m, cr, sr in (("live S2MM", live, R.VDMA_S2MM_CR, R.VDMA_S2MM_SR),
                            ("live MM2S", live, R.VDMA_MM2S_CR, R.VDMA_MM2S_SR),
                            ("vmem MM2S", vmem, R.VDMA_MM2S_CR, R.VDMA_MM2S_SR)):
        done.append(f"{name}: {_stop_channel(m, cr, sr, timeout_s)}")
    return done
