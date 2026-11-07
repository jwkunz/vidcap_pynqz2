"""Host-side tests for the vidcap drivers: no board and no PYNQ needed.

Run from the repository root:   python3 -m unittest discover -s src/drivers/tests -v
"""
import glob
import os
import re
import sys
import unittest
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from vidcap import (FakeMmio, Filter, HeapAllocator, InputSelect, Rtc, Snapshot, StreamMux,  # noqa: E402
                    TimestampOverlay, Vdma, Vidcap, VidcapError, regs as R)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


class TestRtc(unittest.TestCase):
    def test_known_encoding(self):
        # Same words the RTL testbench loads for 2026-11-07 09:41:03
        dt = datetime(2026, 11, 7, 9, 41, 3)
        self.assertEqual(Rtc.encode_date(dt), 0x070B_141A)
        self.assertEqual(Rtc.encode_time(dt), 0x0003_2909)

    def test_roundtrip_edges(self):
        for dt in (datetime(2000, 2, 29, 23, 59, 59), datetime(2099, 12, 31, 0, 0, 0),
                   datetime(1900, 1, 1, 1, 1, 1), datetime(2026, 1, 1, 0, 0, 0)):
            self.assertEqual(Rtc.decode(Rtc.encode_date(dt), Rtc.encode_time(dt)), dt)

    def test_set_sequence_and_error(self):
        m = FakeMmio()
        Rtc(m).set(datetime(2026, 11, 7, 9, 41, 3))
        w = m.writes()
        self.assertEqual(w[-1], (R.RTC_CTRL, R.RTC_CTRL_LOAD), "LOAD must be last")
        self.assertIn((R.RTC_DATE, 0x070B_141A), w)
        m2 = FakeMmio({R.RTC_STATUS: R.RTC_STATUS_ERR})
        with self.assertRaises(VidcapError):
            Rtc(m2).set(datetime(2026, 11, 7))

    def test_get_latches_first(self):
        m = FakeMmio({R.RTC_DATE: 0x070B_141A, R.RTC_TIME: 0x0003_2909})
        self.assertEqual(Rtc(m).get(), datetime(2026, 11, 7, 9, 41, 3))
        self.assertEqual(m.writes()[0], (R.RTC_CTRL, R.RTC_CTRL_LATCH))

    def test_id_check(self):
        with self.assertRaises(VidcapError):
            Rtc(FakeMmio()).check()
        Rtc(FakeMmio({R.RTC_ID: R.RTC_MAGIC})).check()


class TestInputSelect(unittest.TestCase):
    def test_source_and_fallback_bits(self):
        m = FakeMmio({R.ISEL_CTRL: R.ISEL_CTRL_FALLBACK_EN})
        i = InputSelect(m)
        i.set_source("test")
        self.assertEqual(m.regs[R.ISEL_CTRL], R.ISEL_CTRL_FALLBACK_EN | R.ISEL_CTRL_SRC_TPG)
        self.assertEqual(i.source(), "test")
        i.set_fallback(False)
        self.assertEqual(m.regs[R.ISEL_CTRL], R.ISEL_CTRL_SRC_TPG)
        with self.assertRaises(ValueError):
            i.set_source("bogus")

    def test_timeouts_in_clocks(self):
        m = FakeMmio()
        i = InputSelect(m)
        i.set_timeouts(5.0, 1.0)
        self.assertEqual(m.regs[R.ISEL_TIMEOUT], 500_000_000)
        self.assertEqual(m.regs[R.ISEL_RETURN], 100_000_000)
        self.assertEqual(i.timeouts(), (5.0, 1.0))

    def test_status_decode(self):
        m = FakeMmio({R.ISEL_STATUS: 0b01111, R.ISEL_MEASURED: (720 << 16) | 1280})
        st = InputSelect(m).status()
        self.assertTrue(st["hdmi_locked"] and st["hdmi_good"] and st["fallback_active"])
        self.assertEqual(st["active_source"], "test")
        self.assertEqual(st["hdmi_size"], (1280, 720))
        self.assertFalse(st["frame_abort"])


class TestMuxFilterOverlay(unittest.TestCase):
    def test_mux_waits_for_switch(self):
        m = FakeMmio({R.VMUX_STATUS: 0b10})            # pending forever
        with self.assertRaises(VidcapError):
            StreamMux(m).select("vmem", wait_s=0.02)
        m = FakeMmio()
        StreamMux(m).select("vmem")
        self.assertEqual(m.regs[R.VMUX_SEL], 1)

    def test_filter_modes(self):
        m = FakeMmio()
        f = Filter(m)
        f.set_mode("sobel")
        self.assertEqual(m.regs[R.FILT_MODE], 4)
        f.set_mode("threshold", threshold=77)
        self.assertEqual((m.regs[R.FILT_MODE], m.regs[R.FILT_THRESHOLD]), (3, 77))
        self.assertEqual(f.mode(), "threshold")
        for bad in (256, -1):
            with self.assertRaises(ValueError):
                f.set_mode("threshold", threshold=bad)
        with self.assertRaises(KeyError):
            f.set_mode("nope")

    def test_overlay_config(self):
        m = FakeMmio()
        TimestampOverlay(m).configure(True, scale=3, margin=10, fg=(1, 2, 3), bg=None)
        self.assertEqual(m.regs[R.OVL_FG], 0x010203)
        self.assertEqual((m.regs[R.OVL_SCALE], m.regs[R.OVL_MARGIN], m.regs[R.OVL_BG_EN], m.regs[R.OVL_ENABLE]),
                         (3, 10, 0, 1))
        for bad in (0, 5):
            with self.assertRaises(ValueError):
                TimestampOverlay(m).configure(scale=bad)


class TestSnapshot(unittest.TestCase):
    def _hw(self):
        """A fake snapshot core: completes a capture a moment after the trigger changes."""
        m = FakeMmio()
        def on_trigger(v, mm):
            mm.regs[R.SNAP_STATUS] = (((mm.regs.get(R.SNAP_STATUS, 0) >> 8) + 1) & 0xFF) << 8
        m.on_write[R.SNAP_TRIGGER] = on_trigger
        return m

    def test_trigger_wait_ack(self):
        m = self._hw()
        s = Snapshot(m)
        want = s.trigger()
        self.assertEqual(want, 1)
        s.wait(want, timeout_s=0.1)
        self.assertEqual(m.regs[R.SNAP_ACK], 1)
        self.assertFalse(s.irq_pending())

    def test_counters_wrap_at_256(self):
        m = self._hw()
        s = Snapshot(m)
        for _ in range(300):
            s.wait(s.trigger(), timeout_s=0.1)
        self.assertEqual(m.regs[R.SNAP_TRIGGER], 300 & 0xFF)

    def test_timeout_and_buffer_address(self):
        m = FakeMmio()
        s = Snapshot(m)
        with self.assertRaises(VidcapError):
            s.wait(s.trigger(), timeout_s=0.02)
        s.set_buffer(0x1234_5678)
        self.assertEqual((m.regs[R.SNAP_MEM_LO], m.regs[R.SNAP_MEM_HI]), (0x1234_5678, 0))


def vdma_fake(initial=None):
    """A register file that behaves like the VDMA's self-clearing reset bit."""
    m = FakeMmio(initial)
    for cr in (R.VDMA_S2MM_CR, R.VDMA_MM2S_CR):
        m.on_write[cr] = lambda v, mm, cr=cr: mm.regs.__setitem__(cr, v & ~R.VDMA_CR_RESET)
    return m


class TestVdma(unittest.TestCase):
    def _vdma(self):
        m = vdma_fake()
        return Vdma(m), m

    def test_s2mm_start_order(self):
        v, m = self._vdma()
        v.start_s2mm([0x100, 0x200, 0x300])
        w = [(o, val) for o, val in m.writes() if o != R.VDMA_S2MM_CR or val != R.VDMA_CR_RESET]
        offs = [o for o, _ in w]
        self.assertEqual(offs[0], R.VDMA_S2MM_CR)
        self.assertEqual(w[0][1], R.VDMA_CR_RS | R.VDMA_CR_CIRCULAR | R.VDMA_CR_GENLOCK_SRC)
        self.assertEqual(offs[-1], R.VDMA_S2MM_VSIZE, "VSIZE write starts the transfer, so it must be last")
        self.assertEqual(m.regs[R.VDMA_S2MM_ADDR0 + 8], 0x300)
        self.assertEqual((m.regs[R.VDMA_S2MM_HSIZE], m.regs[R.VDMA_S2MM_VSIZE]), (3840, 720))

    def test_mm2s_follow_and_park(self):
        v, m = self._vdma()
        v.start_mm2s([1, 2, 3], genlock_slave=True)
        cr = [val for o, val in m.writes() if o == R.VDMA_MM2S_CR][-1]
        self.assertEqual(cr, R.VDMA_CR_RS | R.VDMA_CR_CIRCULAR | R.VDMA_CR_GENLOCK_EN | R.VDMA_CR_GENLOCK_SRC)
        v, m = self._vdma()
        v.start_mm2s([1, 2], park=1)
        cr = [val for o, val in m.writes() if o == R.VDMA_MM2S_CR][-1]
        self.assertEqual(cr, R.VDMA_CR_RS, "parked = not circular")
        self.assertEqual(m.regs[R.VDMA_PARK_PTR] & 0x1F, 1)

    def test_park_keeps_other_bits(self):
        m = FakeMmio({R.VDMA_PARK_PTR: 0x0302_1F05})
        Vdma(m).park(2)
        self.assertEqual(m.regs[R.VDMA_PARK_PTR], 0x0302_1F02)

    def test_status_decode(self):
        m = FakeMmio({R.VDMA_S2MM_SR: (1 << 0) | (1 << 7) | (1 << 15)})
        st = Vdma(m).status("s2mm")
        self.assertTrue(st["halted"])
        self.assertEqual(st["errors"], ["SOF early", "EOL late"])


class _Traced(FakeMmio):
    """FakeMmio that also records every write, in order, across all blocks."""
    def __init__(self, name, trace, initial=None):
        super().__init__(initial)
        self._name, self._trace = name, trace

    def write(self, offset, value):
        self._trace.append((self._name, offset, value & 0xFFFF_FFFF))
        super().write(offset, value)


def _fake_pipeline():
    ids = {"rtc": {R.RTC_ID: R.RTC_MAGIC}, "isel": {R.ISEL_ID: R.ISEL_MAGIC}, "vmux": {R.VMUX_ID: R.VMUX_MAGIC}}
    by_base, trace = {}, []
    def mmio_for(base):
        name = next(n for n, b in R.BASES.items() if b == base)
        m = _Traced(name, trace, ids.get(name))
        if name.startswith("vdma"):
            for cr in (R.VDMA_S2MM_CR, R.VDMA_MM2S_CR):
                m.on_write[cr] = lambda v, mm, cr=cr: mm.regs.__setitem__(cr, v & ~R.VDMA_CR_RESET)
        by_base[name] = m
        return m
    v = Vidcap(mmio_for, HeapAllocator())
    v.trace = trace
    return v, by_base


class TestPipeline(unittest.TestCase):
    def test_start_programs_everything(self):
        v, mm = _fake_pipeline()
        v.start(sync_time=True)
        live = [f.phys for f in v._live]
        self.assertEqual(len(set(live + [f.phys for f in v._vmem] + [v._snapbuf.phys])), 6, "six distinct buffers")
        self.assertEqual([mm["vdma_live"].regs[R.VDMA_S2MM_ADDR0 + 4 * i] for i in range(3)], live)
        self.assertEqual([mm["vdma_live"].regs[R.VDMA_MM2S_ADDR0 + 4 * i] for i in range(3)], live)
        self.assertEqual(mm["snap"].regs[R.SNAP_MEM_LO], v._snapbuf.phys)
        self.assertEqual(mm["filt"].regs[R.FILT_MODE], 0, "powers up in passthrough")
        self.assertEqual(mm["ovl"].regs[R.OVL_ENABLE], 1)
        self.assertEqual(mm["isel"].regs[R.ISEL_CTRL], R.ISEL_CTRL_FALLBACK_EN)
        self.assertIn((R.RTC_CTRL, R.RTC_CTRL_LOAD), mm["rtc"].writes())
        # the writer must be started before the reader that follows it, and the controls before both
        pos = {key: next(i for i, (n, o, _) in enumerate(v.trace) if (n, o) == key)
               for key in (("vdma_live", R.VDMA_S2MM_VSIZE), ("vdma_live", R.VDMA_MM2S_VSIZE),
                           ("filt", R.FILT_MODE), ("snap", R.SNAP_MEM_LO))}
        self.assertLess(pos[("filt", R.FILT_MODE)], pos[("vdma_live", R.VDMA_S2MM_VSIZE)])
        self.assertLess(pos[("snap", R.SNAP_MEM_LO)], pos[("vdma_live", R.VDMA_S2MM_VSIZE)])
        self.assertLess(pos[("vdma_live", R.VDMA_S2MM_VSIZE)], pos[("vdma_live", R.VDMA_MM2S_VSIZE)])

    def test_wrong_bitstream_fails_early(self):
        def mmio_for(base):
            return FakeMmio()                           # every ID reads 0
        with self.assertRaises(VidcapError):
            Vidcap(mmio_for, HeapAllocator()).start()

    def test_show_image_validates_and_flips(self):
        v, mm = _fake_pipeline()
        v.start(sync_time=False)
        with self.assertRaises(ValueError):
            v.show_image(np.zeros((10, 10, 3), np.uint8))
        img = np.zeros((R.HEIGHT, R.WIDTH, 3), np.uint8)
        img[0, 0] = (1, 2, 3)
        v.show_image(img, rgb=True)
        self.assertEqual(tuple(v._vmem[1].array[0, 0]), (3, 2, 1), "rgb=True reverses to B,G,R")
        self.assertEqual(mm["vdma_vmem"].regs[R.VDMA_PARK_PTR] & 0x1F, 1)
        self.assertEqual(mm["vmux"].regs[R.VMUX_SEL], 1)
        v.show_image(img)
        self.assertEqual(mm["vdma_vmem"].regs[R.VDMA_PARK_PTR] & 0x1F, 0, "double buffer alternates")

    def test_snapshot_returns_a_copy(self):
        v, mm = _fake_pipeline()
        v.start(sync_time=False)
        def on_trigger(val, m):
            m.regs[R.SNAP_STATUS] = (((m.regs.get(R.SNAP_STATUS, 0) >> 8) + 1) & 0xFF) << 8
            v._snapbuf.array[5, 5] = (9, 8, 7)
        mm["snap"].on_write[R.SNAP_TRIGGER] = on_trigger
        img = v.snapshot(timeout_s=0.1)
        self.assertEqual(tuple(img[5, 5]), (9, 8, 7))
        v._snapbuf.array[5, 5] = 0
        self.assertEqual(tuple(img[5, 5]), (9, 8, 7), "result must not alias the DMA buffer")


class TestRegisterMapsMatchGenerated(unittest.TestCase):
    """The HLS tools generate the true register offsets; they must agree with regs.py."""

    def _defines(self, core):
        pat = os.path.join(ROOT, "workspace", "vitishls", f"{core}_build", "work", "hls", "impl", "ip", "drivers",
                           f"{core}_v1_0", "src", f"x{core}_hw.h")
        files = glob.glob(pat)
        if not files:
            self.skipTest(f"{core} has not been packaged (run its package step first)")
        out = {}
        with open(files[0]) as fh:
            lines = fh.readlines()
        for line in lines:
            m = re.match(r"#define XVC_\w+?_CONTROL_ADDR_(\w+?)(?:_DATA)?\s+0x([0-9a-fA-F]+)", line)
            if m:
                out[m.group(1)] = int(m.group(2), 16)
        return out

    def test_filter(self):
        d = self._defines("vc_filter")
        self.assertEqual((d["MODE"], d["THRESHOLD"]), (R.FILT_MODE, R.FILT_THRESHOLD))

    def test_overlay(self):
        d = self._defines("vc_overlay")
        got = {k: d[k] for k in ("ENABLE", "SCALE", "MARGIN", "FG", "BG", "BG_EN")}
        self.assertEqual(got, {"ENABLE": R.OVL_ENABLE, "SCALE": R.OVL_SCALE, "MARGIN": R.OVL_MARGIN,
                               "FG": R.OVL_FG, "BG": R.OVL_BG, "BG_EN": R.OVL_BG_EN})

    def test_snapshot(self):
        d = self._defines("vc_snapshot")
        self.assertEqual((d["MEM"], d["TRIGGER"], d["ACK"], d["STATUS"]),
                         (R.SNAP_MEM_LO, R.SNAP_TRIGGER, R.SNAP_ACK, R.SNAP_STATUS))
        self.assertEqual(R.SNAP_MEM_HI, R.SNAP_MEM_LO + 4)


if __name__ == "__main__":
    unittest.main()
