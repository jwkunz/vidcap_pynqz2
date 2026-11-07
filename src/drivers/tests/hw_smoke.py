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


W, H = R.WIDTH, R.HEIGHT


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
    w = W // 8
    for i in range(8):
        samples = [tuple(int(c) for c in img[y, i * w + w // 2]) for y in rows]
        out.append(max(set(samples), key=samples.count))
    return out


def main():
    global W, H
    mode = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("VIDCAP_MODE", R.DEFAULT_MODE)
    geo = R.MODES[mode]
    W, H = geo.width, geo.height
    say(f"== load the {mode} bitstream ({W}x{H})")
    t0 = time.time()
    v = Vidcap.on_pynq(mode=mode)
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
        rings = [bar_colours(fb.array, [10, H - 20]) == BARS_BGR for fb in v._live]
        check("DMA wrote the colour bars into all 3 live-ring frames", all(rings), f"{sum(rings)} of 3")

        say("== snapshot of the test pattern")
        t0 = time.time()
        img = v.snapshot()
        say(f"   took {1000 * (time.time() - t0):.0f} ms")
        check("snapshot has the right shape", img.shape == (H, W, 3), str(img.shape))
        rows = [10, H - 20, H - 10]            # rows the moving box can only partly reach
        got = bar_colours(img, [r for r in rows if r < H])
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
        grad = np.zeros((H, W, 3), np.uint8)
        grad[..., 0] = np.linspace(0, 255, W, dtype=np.uint8)
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

        say("== noise filter (register writes accepted; the grain is on the monitor)")
        for amount in (0, 60, 255):
            v.set_filter("noise", noise=amount)
            time.sleep(0.2)
        a = v.input.status()["out_frames"]
        time.sleep(0.5)
        check("video keeps flowing at full noise", v.input.status()["out_frames"] - a >= 20)
        check("noise setting reads back", v.filter.noise() == 255 and v.filter.mode() == "noise")
        v.set_filter("passthrough")

        say("== temporal average (running sum in DDR)")
        v.input.set_source("test")                     # the colour bars are static, so the DDR contents are predictable
        time.sleep(0.5)
        v.set_temporal_average(16)
        time.sleep(1.2)                                # 16 frames fit in well under a second
        st = v.tavg.status()
        check("the average window fills to N=16", st["depth"] == 16, str(st))
        a = v.input.status()["out_frames"]
        time.sleep(0.5)
        check("video keeps flowing at about 60 fps with the average on", v.input.status()["out_frames"] - a >= 25,
              f"{v.input.status()['out_frames'] - a} frames in 0.5 s")
        for name, ch in (("live S2MM", v.vdma_live.status("s2mm")), ("live MM2S", v.vdma_live.status("mm2s"))):
            check(f"VDMA {name} still without errors", not ch["errors"], str(ch["errors"]))
        # what the hardware wrote: the history ring holds brightness, the sum frame holds 16 x brightness
        buf = v._tavgbuf.array
        slots = v.history_frames
        ring = buf[:slots * geo.luma_frame_bytes].reshape(slots, H, W)
        sums = buf[slots * geo.luma_frame_bytes:].view("<u2").reshape(H, W)
        bar_rgb = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
        bar_luma = [(77 * r + 150 * g + 29 * b) >> 8 for r, g, b in bar_rgb]
        w8 = W // 8
        samples = [(y, i) for i in range(8) for y in range(8, H, 40)]      # the moving box covers well under 5%
        ring_ok = sum(all(int(ring[k, y, i * w8 + w8 // 2]) == bar_luma[i] for k in range(slots)) for y, i in samples)
        sum_ok = sum(int(sums[y, i * w8 + w8 // 2]) == 16 * bar_luma[i] for y, i in samples)
        check("history ring holds the brightness of the colour bars in all 16 slots", ring_ok >= 0.9 * len(samples),
              f"{ring_ok} of {len(samples)} sampled pixels")
        check("sum frame holds exactly 16 x brightness", sum_ok >= 0.9 * len(samples), f"{sum_ok} of {len(samples)} sampled pixels")
        v.set_temporal_average(4)                      # changing N restarts the window
        time.sleep(0.3)
        st = v.tavg.status()
        check("changing N restarts the window and it refills to 4", st["depth"] == 4, str(st))
        v.set_temporal_average(None)
        time.sleep(0.2)
        check("the average switches off", not v.tavg.enabled())
        v.input.set_source("hdmi")

        say("== colour grading (look-up table): checked pixel by pixel against the bit-exact model")
        if v.has_lut:
            from vidcap import lut as L
            v.lut.check()
            check("colour grading block answers with the right ID", True)
            v.set_filter("passthrough")
            v.set_temporal_average(None)
            colours = [(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255), (16, 16, 16), (15, 240, 99),
                       (200, 120, 40), (33, 77, 221), (128, 128, 128), (250, 5, 130), (1, 254, 17)]

            def probe_all(cube, strength_code, label):
                ok_in = ok_out = 0
                bad = []
                for rgb in colours:
                    img = np.zeros((H, W, 3), np.uint8)
                    img[..., 0], img[..., 1], img[..., 2] = rgb[2], rgb[1], rgb[0]       # memory order B, G, R
                    v.show_image(img)
                    time.sleep(0.12)
                    pin, pout = v.lut.probe()
                    want = tuple(int(x) for x in L.apply_lut(np.array(rgb, np.uint8), cube, strength_code))
                    ok_in += pin == rgb
                    ok_out += pout == want
                    if pout != want:
                        bad.append((rgb, pout, want))
                check(f"{label}: the table sees the colour that was shown", ok_in == len(colours), f"{ok_in} of {len(colours)}")
                check(f"{label}: output equals the model for every colour", ok_out == len(colours),
                      f"{ok_out} of {len(colours)}" + (f"; first mismatch {bad[0]}" if bad else ""))

            v.set_look("teal_orange", strength=100)
            check("a built-in look loads and verifies (4913 nodes, checksum)", v.lut.enabled() and v.look == "teal_orange")
            probe_all(L.build_look("teal_orange"), 256, "teal_orange at 100 %")
            v.set_look_strength(50)
            time.sleep(0.1)
            probe_all(L.build_look("teal_orange"), 128, "teal_orange at 50 %")
            rnd = np.random.default_rng(5).integers(0, 256, (17, 17, 17, 3), dtype=np.uint8)
            v.set_look(rnd, strength=100)
            probe_all(rnd, 256, "a random table (checks every bank and the address layout)")
            v.set_look(None)
            time.sleep(0.1)
            probe_all(L.identity_cube(), 0, "grading off")
            v.show_live()
        else:
            say("   this bitstream has no colour grading: skipped")

        say("== HDMI input")
        v.use_test_pattern(False)
        time.sleep(1.5)
        st = v.input.status()
        if st["hdmi_good"]:
            say(f"   a live {W}x{H} HDMI source is connected: checking passthrough instead of the no-signal fallback")
            a = v.input.status()
            time.sleep(1.0)
            b = v.input.status()
            check("HDMI frames arrive at about 60/s", 50 <= b["hdmi_frames"] - a["hdmi_frames"] <= 70,
                  f"{b['hdmi_frames'] - a['hdmi_frames']} frames in 1 s")
            check("HDMI is what is being shown", b["active_source"] == "hdmi" and not b["fallback_active"])
            check("output runs at about 60 fps from HDMI", 50 <= b["out_frames"] - a["out_frames"] <= 70)
        else:
            say("   fallback: HDMI selected with no signal connected")
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
