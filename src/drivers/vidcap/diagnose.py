"""Read-only health check of a running vidcap design.

    sudo python3 -m vidcap.diagnose          (from the folder that contains the vidcap package)

Reads status registers through /dev/mem for about a second and says where the video stopped, if it did.
It never loads, starts or changes anything, so it is safe to run while a notebook is using the board,
and it does not need the notebook's Vidcap object.

Only run it when a vidcap design is loaded: reading registers of an unconfigured FPGA can hang the CPU.
"""
from __future__ import annotations

import sys
import time
from typing import Callable, Dict, List

from . import regs as R
from .backend import DevMemMmio, Mmio


def sample(mmio_for: Callable[[int], Mmio] = DevMemMmio, seconds: float = 1.0) -> Dict[str, object]:
    """Read the registers twice, `seconds` apart, and return values and per-second rates."""
    m = {n: mmio_for(b) for n, b in R.BASES.items()}

    def once():
        isel, snap, vl = m["isel"], m["snap"], m["vdma_live"]
        st = isel.read(R.ISEL_STATUS)
        meas = isel.read(R.ISEL_MEASURED)
        return {
            "ctrl": isel.read(R.ISEL_CTRL), "status": st, "w": meas & 0xFFFF, "h": meas >> 16,
            "hdmi_frames": isel.read(R.ISEL_HDMI_FRAMES), "out_frames": isel.read(R.ISEL_OUT_FRAMES),
            "mux_frames": m["vmux"].read(R.VMUX_FRAMES), "mux_status": m["vmux"].read(R.VMUX_STATUS),
            "s2mm_sr": vl.read(R.VDMA_S2MM_SR), "mm2s_sr": vl.read(R.VDMA_MM2S_SR),
            "vmem_sr": m["vdma_vmem"].read(R.VDMA_MM2S_SR),
            "snap_status": snap.read(R.SNAP_STATUS), "snap_trigger": snap.read(R.SNAP_TRIGGER),
            "snap_ack": snap.read(R.SNAP_ACK), "filter": m["filt"].read(R.FILT_MODE) & 7,
            "overlay": m["ovl"].read(R.OVL_ENABLE) & 1,
        }

    a = once()
    time.sleep(seconds)
    b = once()
    rate = lambda k: (b[k] - a[k]) / seconds                                    # noqa: E731
    b.update(rate_hdmi=rate("hdmi_frames"), rate_out=rate("out_frames"), rate_mux=rate("mux_frames"))
    return b


def verdict(s: Dict[str, object]) -> List[str]:
    """Plain-language findings from a sample(), most important first."""
    out: List[str] = []
    st = s["status"]
    locked, good, fallback, tpg = bool(st & 1), bool(st & 2), bool(st & 4), bool(st & 8)
    wants_tpg = bool(s["ctrl"] & 1) or fallback
    flowing = s["rate_out"] > 5
    done = (s["snap_status"] >> 8) & 0xFF

    if not flowing and (s["rate_hdmi"] > 5 or wants_tpg or tpg):
        out.append("STALLED: video is available at the input but the input select emits no frames. The stream "
                   "is blocked downstream of it (snapshot core or the live write DMA is not accepting data). "
                   "If a DMA reset also times out, the board's memory port is wedged: stop the notebooks, "
                   "then reboot the board.")
        if s["s2mm_sr"] & R.VDMA_SR_HALTED:
            out.append("the live write DMA (S2MM) is halted: it was never started or was stopped.")
        if wants_tpg != tpg:
            out.append("the input select wants the " + ("test pattern" if wants_tpg else "HDMI input") +
                       " but is stuck on the other one, because it only switches between frames.")
    if s["snap_trigger"] != done and not flowing:
        out.append(f"snapshots were requested ({s['snap_trigger']}) but {done} completed: expected while the "
                   "stream is stalled, because the snapshot core only runs when frames arrive.")
    if not locked and not tpg:
        out.append("HDMI: no signal locked. Check the cable, the source, and that it outputs 1280x720.")
    elif locked and not good and s["rate_hdmi"] < 5:
        out.append("HDMI is locked but no frames arrive: the source stopped, or the stream is back-pressured.")
    elif locked and not good:
        out.append(f"HDMI frames arrive but are not accepted as 1280x720 (last frame {s['w']}x{s['h']}).")
    if fallback:
        out.append("fallback is active: HDMI was bad for 5 s, so the test pattern is being shown.")
    for name, sr in (("live write DMA", s["s2mm_sr"]), ("live read DMA", s["mm2s_sr"]), ("video-memory DMA", s["vmem_sr"])):
        errs = [n for bit, n in R.VDMA_SR_ERRORS.items() if sr & (1 << bit)]
        if errs:
            out.append(f"{name} reports: {', '.join(errs)} (sticky; start-up glitches are cleared by Vidcap.start()).")
    if flowing and not out:
        out.append(f"OK: video is flowing ({s['rate_out']:.0f} frames/s from {'the test pattern' if tpg else 'HDMI'}).")
    return out


def main() -> int:
    s = sample()
    st = s["status"]
    print(f"input select : source={'test' if s['ctrl'] & 1 else 'hdmi'} fallback_en={bool(s['ctrl'] & 2)} "
          f"active={'test' if st & 8 else 'hdmi'} locked={bool(st & 1)} good={bool(st & 2)} last frame {s['w']}x{s['h']}")
    print(f"frames/s     : hdmi in {s['rate_hdmi']:.0f}   input select out {s['rate_out']:.0f}   "
          f"mux {s['rate_mux']:.0f}   mux shows {'video memory' if s['mux_status'] & 1 else 'live video'}")
    print(f"snapshot     : triggered {s['snap_trigger']}, completed {(s['snap_status'] >> 8) & 255}, acknowledged "
          f"{s['snap_ack']}, busy {s['snap_status'] & 1}")
    print(f"filter mode  : {s['filter']}   timestamp {'on' if s['overlay'] else 'off'}")
    print("\nfindings:")
    for line in verdict(s):
        print("  -", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
