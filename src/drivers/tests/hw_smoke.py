#!/usr/bin/env python3
"""On-board smoke test for the vidcap overlay. Needs root (PYNQ) and the bitstream; no HDMI source.

    cd /home/xilinx/jupyter_notebooks/vidcap
    sudo /usr/local/share/pynq-venv/bin/python3 hw_smoke.py

Uses only the built-in test pattern as its video source. Every step prints as it happens (flushed), so if
the board stops responding the last line says where. The DMA engines are always stopped on exit; leaving
them running would let the hardware write into memory that Linux has already freed.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np                                               # noqa: E402

from vidcap import Vidcap, regs as R                             # noqa: E402

results = []


def say(msg):
    print(msg, flush=True)


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    say(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    return ok


# colour bars in memory byte order B, G, R
BARS_BGR = [(255, 255, 255), (0, 255, 255), (255, 255, 0), (0, 255, 0),
            (255, 0, 255), (0, 0, 255), (255, 0, 0), (0, 0, 0)]


def bar_colours(img, rows):
    """Colour at the centre of each of the 8 bars, taking the value most rows agree on."""
    out = []
    w = R.WIDTH // 8
    for i in range(8):
        samples = [tuple(int(c) for c in img[y, i * w + w // 2]) for y in rows]
        out.append(max(set(samples), key=samples.count))
    return out


def main():
    bit = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vidcap.bit")
    say(f"== load {bit}")
    t0 = time.time()
    v = Vidcap.on_pynq(bit)
    say(f"   downloaded in {time.time() - t0:.2f} s")
    try:
        say("== block IDs (first AXI reads)")
        v.check()
        check("rtc / input_sel / stream_mux answer with the right IDs", True)
        say(f"   VDMA live version register: 0x{v.vdma_live.version():08X}")

        say("== start with the test pattern")
        v.start(source="test", fallback=True, sync_time=True)
        time.sleep(1.0)
        st = v.input.status()
        check("test pattern is the active source", st["active_source"] == "test", str(st["active_source"]))

        a = (st["out_frames"], v.mux.frames())
        time.sleep(1.0)
        b = (v.input.status()["out_frames"], v.mux.frames())
        check("input select emits frames at about 60 fps", 45 <= b[0] - a[0] <= 75, f"{b[0] - a[0]} frames in 1 s")
        check("live reader delivers frames to the filter chain", 45 <= b[1] - a[1] <= 75, f"{b[1] - a[1]} frames in 1 s")

        for name, ch in (("live S2MM", v.vdma_live.status("s2mm")), ("live MM2S", v.vdma_live.status("mm2s"))):
            check(f"VDMA {name} running without errors after start-up", not ch["halted"] and not ch["errors"],
                  f"halted={ch['halted']} errors={ch['errors']} raw=0x{ch['raw']:08X}")
        # what the DMA itself wrote into the live ring (not the snapshot core's copy)
        rings = [bar_colours(fb.array, [10, 700]) == BARS_BGR for fb in v._live]
        check("DMA wrote the colour bars into all 3 live-ring frames", all(rings), f"{sum(rings)} of 3")

        say("== snapshot of the test pattern")
        t0 = time.time()
        img = v.snapshot()
        say(f"   took {1000 * (time.time() - t0):.0f} ms")
        check("snapshot has the right shape", img.shape == (R.HEIGHT, R.WIDTH, 3), str(img.shape))
        rows = [10, 700, 710]            # rows the moving box can only partly reach
        got = bar_colours(img, [r for r in rows if r < R.HEIGHT])
        check("8 colour bars in the right order and byte order (B,G,R)", got == BARS_BGR,
              "" if got == BARS_BGR else f"got {got}")
        img2 = v.snapshot()
        check("the moving box moves between snapshots", int(np.abs(img.astype(int) - img2.astype(int)).sum()) > 0)
        white_box = int(((img == 255).all(axis=2)).sum())
        say(f"   white pixels in snapshot: {white_box}")
        for _ in range(3):
            v.snapshot()
        check("repeated snapshots keep working", v.snap.done_count() >= 5, f"done count {v.snap.done_count()}")
        check("snapshot interrupt is acknowledged", not v.snap.irq_pending())

        say("== filters (register writes accepted; picture is on the monitor)")
        for mode in ("invert", "grayscale", "threshold", "sobel", "blur", "sharpen", "passthrough"):
            v.set_filter(mode, threshold=100)
            time.sleep(0.15)
        check("filter mode reads back", v.filter.mode() == "passthrough", v.filter.mode())

        say("== video memory (double-buffered flip)")
        grad = np.zeros((R.HEIGHT, R.WIDTH, 3), np.uint8)
        grad[..., 0] = np.linspace(0, 255, R.WIDTH, dtype=np.uint8)
        v.show_image(grad)
        time.sleep(0.5)
        f0 = v.mux.frames()
        time.sleep(0.5)
        check("mux switched to video memory", v.mux.active() == "vmem", v.mux.active())
        check("video-memory reader delivers frames", v.mux.frames() - f0 >= 20, f"{v.mux.frames() - f0} frames in 0.5 s")
        st = v.vdma_vmem.status("mm2s")
        check("VDMA video memory running without errors", not st["halted"] and not st["errors"],
              f"halted={st['halted']} errors={st['errors']} raw=0x{st['raw']:08X}")
        v.show_image(grad[:, ::-1].copy())
        check("second buffer flip accepted", v.mux.active() == "vmem")
        v.show_live()
        check("back to live video", v.mux.active() == "live")

        say("== timestamp and hardware clock")
        v.overlay.configure(scale=2, margin=16)
        now = v.sync_time()
        time.sleep(2.2)
        drift = abs(v.rtc.drift_against())
        check("hardware clock tracks the board clock", drift < 1.5, f"{drift:.2f} s apart after set + 2 s")
        check("clock reports it was set", v.rtc.was_set)

        say("== fallback: HDMI selected with no signal connected")
        v.use_test_pattern(False)
        time.sleep(1.0)
        st = v.input.status()
        check("HDMI input reported not good while nothing is connected", not st["hdmi_good"], f"locked={st['hdmi_locked']}")
        say("   waiting for the 5 s fallback timeout ...")
        time.sleep(5.5)
        st = v.input.status()
        check("test pattern took over after 5 s", st["fallback_active"] and st["active_source"] == "test",
              f"fallback={st['fallback_active']} source={st['active_source']}")
        a = v.input.status()["out_frames"]
        time.sleep(0.5)
        check("output keeps running during fallback", v.input.status()["out_frames"] - a >= 20)

        say("== final state")
        for k, val in v.status().items():
            say(f"   {k}: {val}")
    finally:
        say("== stopping DMA engines")
        try:
            v.stop()
        except Exception as e:                                   # report, do not hide the real failure
            say(f"   stop() failed: {e}")

    failed = [n for n, ok in results if not ok]
    say(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    for n in failed:
        say(f"  FAILED: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
