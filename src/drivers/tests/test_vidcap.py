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
            for sr in (R.VDMA_S2MM_SR, R.VDMA_MM2S_SR):      # status bits are write-1-to-clear
                m.on_write[sr] = lambda v, mm, sr=sr, prev=None: mm.regs.__setitem__(sr, 0)
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

    def test_start_clears_the_startup_error_flags(self):
        v, mm = _fake_pipeline()
        v.start(sync_time=False)
        mask = sum(1 << b for b in R.VDMA_SR_ERRORS)
        self.assertIn((R.VDMA_S2MM_SR, mask), mm["vdma_live"].writes())
        self.assertIn((R.VDMA_MM2S_SR, mask), mm["vdma_live"].writes())
        self.assertIn((R.VDMA_MM2S_SR, mask), mm["vdma_vmem"].writes())
        # ... and only after the engines were started
        w = mm["vdma_live"].writes()
        self.assertLess(w.index((R.VDMA_S2MM_VSIZE, 720)), w.index((R.VDMA_S2MM_SR, mask)))

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


class TestTimeZone(unittest.TestCase):
    def _clock(self, mm):
        return Rtc.decode(mm["rtc"].regs[R.RTC_DATE], mm["rtc"].regs[R.RTC_TIME])

    def test_zone_changes_the_displayed_time(self):
        v, mm = _fake_pipeline()
        v.sync_time("UTC")
        utc = self._clock(mm)
        v.sync_time("Asia/Tokyo")
        tokyo = self._clock(mm)
        self.assertAlmostEqual((tokyo - utc).total_seconds(), 9 * 3600, delta=5)
        self.assertEqual(v.tz, "Asia/Tokyo", "remembered for later syncs")
        v.sync_time("")                                   # back to the board's own zone
        self.assertIsNone(v.tz)

    def test_start_accepts_a_zone(self):
        v, mm = _fake_pipeline()
        v.start(tz="America/New_York")
        from zoneinfo import ZoneInfo
        expect = datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
        self.assertAlmostEqual((self._clock(mm) - expect).total_seconds(), 0, delta=5)

    def test_unknown_zone_is_a_clear_error(self):
        v, _ = _fake_pipeline()
        with self.assertRaises(ValueError) as cm:
            v.sync_time("Mars/Olympus_Mons")
        self.assertIn("IANA", str(cm.exception))

    def test_drift_is_measured_in_the_same_zone(self):
        v, mm = _fake_pipeline()
        v.sync_time("Asia/Tokyo")
        self.assertLess(abs(v.clock_drift()), 3)          # would be about 9 hours if it compared with local time


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


# ---------------------------------------------------------------------------------------------
try:
    import ipywidgets  # noqa: F401
    HAVE_WIDGETS = True
except ImportError:
    HAVE_WIDGETS = False


@unittest.skipUnless(HAVE_WIDGETS, "ipywidgets not installed")
class TestPanel(unittest.TestCase):
    def setUp(self):
        import tempfile
        from vidcap import ui
        self.ui = ui
        self.v, self.mm = _fake_pipeline()
        self.v.start(sync_time=False)
        self.mm["rtc"].regs[R.RTC_DATE] = 0x070B_141A
        self.mm["rtc"].regs[R.RTC_TIME] = 0x0003_2909

        def on_trigger(val, m):                       # a snapshot core that completes at once
            m.regs[R.SNAP_STATUS] = (((m.regs.get(R.SNAP_STATUS, 0) >> 8) + 1) & 0xFF) << 8
            self.v._snapbuf.array[:] = 0
            self.v._snapbuf.array[10, 20] = (3, 2, 1)
        self.mm["snap"].on_write[R.SNAP_TRIGGER] = on_trigger
        self.tmp = tempfile.mkdtemp()
        self.panel = ui.build_panel(self.v, refresh_s=0.05, snapshot_dir=self.tmp)
        self.w = self.panel.widgets
        self.addCleanup(self.panel.stop)

    def log_text(self):
        return "\n".join(self.panel.messages)

    def test_filter_controls(self):
        self.w["mode"].value = "sobel"
        self.assertEqual(self.mm["filt"].regs[R.FILT_MODE], 4)
        self.w["threshold"].value = 77
        self.assertEqual(self.mm["filt"].regs[R.FILT_THRESHOLD], 77)
        self.assertEqual(self.mm["filt"].regs[R.FILT_MODE], 4, "mode kept when only the threshold moves")

    def test_source_and_fallback(self):
        self.w["source"].value = "test"
        self.assertTrue(self.mm["isel"].regs[R.ISEL_CTRL] & R.ISEL_CTRL_SRC_TPG)
        self.w["fallback"].value = False
        self.assertFalse(self.mm["isel"].regs[R.ISEL_CTRL] & R.ISEL_CTRL_FALLBACK_EN)
        self.w["source"].value = "hdmi"
        self.assertFalse(self.mm["isel"].regs[R.ISEL_CTRL] & R.ISEL_CTRL_SRC_TPG)

    def test_timestamp_controls(self):
        self.w["ts_scale"].value = 3
        self.w["ts_margin"].value = 40
        self.w["ts_fg"].value = "#ff8000"
        r = self.mm["ovl"].regs
        self.assertEqual((r[R.OVL_SCALE], r[R.OVL_MARGIN], r[R.OVL_FG]), (3, 40, 0xFF8000))
        self.assertEqual(r[R.OVL_BG_EN], 1)
        self.w["ts_clear"].value = True
        self.assertEqual(r[R.OVL_BG_EN], 0, "transparent background")
        self.w["ts_on"].value = False
        self.assertEqual(r[R.OVL_ENABLE], 0)

    def test_video_memory_and_revert(self):
        self.w["display"].value = "vmem"
        self.assertEqual(self.mm["vmux"].regs[R.VMUX_SEL], 1)
        self.assertEqual(self.mm["vdma_vmem"].regs[R.VDMA_PARK_PTR] & 0x1F, 1, "wrote the hidden buffer and flipped")
        self.w["display"].value = "live"
        self.assertEqual(self.mm["vmux"].regs[R.VMUX_SEL], 0)
        # no snapshot yet: choosing it must fail visibly and put the toggle back
        self.w["image"].value = "Last snapshot"
        self.w["display"].value = "vmem"
        self.assertEqual(self.w["display"].value, "live")
        self.assertIn("no snapshot yet", self.log_text())

    def test_snapshot_and_save(self):
        self.assertTrue(self.w["save"].disabled)
        self.w["snap"].click()
        self.assertFalse(self.w["save"].disabled)
        self.assertEqual(self.panel.widgets["snap_info"].value[:8], "captured")
        self.assertEqual(self.w["snap"].description, "Capture snapshot")
        self.assertTrue(bytes(self.w["snap_img"].value).startswith(b"\x89PNG"), "preview is a PNG")
        self.w["save"].click()
        saved = os.listdir(self.tmp)
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0].startswith("snap_") and saved[0].endswith(".png"))
        # a saved snapshot can be sent back to the display
        self.w["image"].value = "Last snapshot"
        self.w["display"].value = "vmem"
        self.assertEqual(self.w["display"].value, "vmem")

    def test_status_text(self):
        self.mm["isel"].regs[R.ISEL_STATUS] = R.ISEL_ST_LOCKED | R.ISEL_ST_GOOD
        self.mm["isel"].regs[R.ISEL_MEASURED] = (720 << 16) | 1280
        self.panel.refresh()
        text = self.w["status"].value
        self.assertIn("good", text)
        self.assertIn("1280x720", text)
        self.assertIn("2026-11-07 09:41:03", text)
        self.assertIn("none", text, "no DMA errors")
        self.mm["vdma_live"].regs[R.VDMA_S2MM_SR] = 1 << 7
        self.panel.refresh()
        self.assertIn("SOF early", self.w["status"].value)

    @unittest.skipUnless(HAVE_WIDGETS, "ipywidgets not installed")
    def test_panel_time_zone_box(self):
        self.w["tz"].value = "Asia/Tokyo"
        self.w["sync"].click()
        self.assertEqual(self.v.tz, "Asia/Tokyo")
        self.w["tz"].value = "Nowhere/Land"
        self.w["sync"].click()
        self.assertIn("unknown time zone", self.log_text())
        self.w["tz"].value = ""
        self.w["sync"].click()
        self.assertIsNone(self.v.tz)

    def test_status_survives_garbage_clock(self):
        self.mm["rtc"].regs[R.RTC_DATE] = 0
        self.panel.refresh()
        self.assertIn("unreadable", self.w["status"].value)

    def test_background_refresh_runs_and_stops(self):
        import time as _t
        self.w["status"].value = "stale"
        _t.sleep(0.3)
        self.assertNotEqual(self.w["status"].value, "stale", "the refresh thread updates the status")
        self.panel.stop()


class TestPanelHelpers(unittest.TestCase):
    def test_images_have_the_right_shape(self):
        from vidcap import ui
        for make in (ui.image_gradient, ui.image_bars, ui.image_checker):
            img = make()
            self.assertEqual((img.shape, img.dtype), ((R.HEIGHT, R.WIDTH, 3), np.dtype("uint8")))

    def test_bars_are_bgr_in_memory(self):
        from vidcap import ui
        bars = ui.image_bars()
        self.assertEqual(tuple(bars[0, 0]), (255, 255, 255), "white")
        self.assertEqual(tuple(bars[0, R.WIDTH // 8 + 1]), (0, 255, 255), "yellow = B 0, G 255, R 255")
        self.assertEqual(tuple(bars[0, 5 * (R.WIDTH // 8) + 1]), (0, 0, 255), "red = R 255 in the last byte")

    def test_hex_colour(self):
        from vidcap import ui
        self.assertEqual(ui.hex_to_rgb("#ff8000"), (255, 128, 0))


class TestSafety(unittest.TestCase):
    def test_trusts_pynq_state_only_if_written_after_boot(self):
        import tempfile
        from vidcap import safety
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        mtime = os.path.getmtime(f.name)
        name = "/home/xilinx/jupyter_notebooks/vidcap/vidcap.bit"
        self.assertTrue(safety.design_loaded_since_boot("vidcap.bit", f.name, mtime - 100, name))
        self.assertFalse(safety.design_loaded_since_boot("vidcap.bit", f.name, mtime + 100, name),
                         "state file older than this boot: the FPGA was reset, do not touch it")
        self.assertFalse(safety.design_loaded_since_boot("vidcap.bit", f.name, mtime - 100, "/x/other.bit"))
        self.assertFalse(safety.design_loaded_since_boot("vidcap.bit", "/nonexistent/state", 0, name),
                         "no state file: not loaded")
        os.unlink(f.name)

    def _vdma_pair(self, halts_on_rs_clear=True, honours_reset=True):
        """Fake VDMAs whose channels behave like (or deliberately unlike) the real ones."""
        mm = {}

        def make(name):
            m = FakeMmio()
            m.regs[R.VDMA_S2MM_CR] = m.regs[R.VDMA_MM2S_CR] = R.VDMA_CR_RS
            for cr, sr in ((R.VDMA_S2MM_CR, R.VDMA_S2MM_SR), (R.VDMA_MM2S_CR, R.VDMA_MM2S_SR)):
                m.regs[sr] = 0                                              # running

                def on_write(v, mm_, cr=cr, sr=sr):
                    if v & R.VDMA_CR_RESET:
                        if honours_reset:
                            mm_.regs[sr] = R.VDMA_SR_HALTED
                            mm_.regs[cr] = v & ~R.VDMA_CR_RESET
                    elif not v & R.VDMA_CR_RS and halts_on_rs_clear:
                        mm_.regs[sr] = R.VDMA_SR_HALTED
                m.on_write[cr] = on_write
            mm[name] = m
            return m

        def mmio_for(base):
            name = next(n for n, b in R.BASES.items() if b == base)
            return mm.get(name) or make(name)
        return mmio_for, mm

    def test_stops_every_dma_channel(self):
        from vidcap import safety
        mmio_for, mm = self._vdma_pair()
        done = safety.stop_running_design(mmio_for, timeout_s=0.05)
        self.assertEqual(len(done), 3)
        self.assertTrue(all(d.endswith("stopped") for d in done), done)
        self.assertTrue(mm["vdma_live"].regs[R.VDMA_S2MM_SR] & R.VDMA_SR_HALTED)
        self.assertTrue(mm["vdma_vmem"].regs[R.VDMA_MM2S_SR] & R.VDMA_SR_HALTED)

    def test_forces_a_channel_that_will_not_halt(self):
        from vidcap import safety
        mmio_for, _ = self._vdma_pair(halts_on_rs_clear=False)
        done = safety.stop_running_design(mmio_for, timeout_s=0.05)
        self.assertTrue(all(d.endswith("reset") for d in done), done)

    def test_refuses_to_continue_if_a_dma_cannot_be_stopped(self):
        from vidcap import safety
        mmio_for, _ = self._vdma_pair(halts_on_rs_clear=False, honours_reset=False)
        with self.assertRaises(VidcapError) as cm:
            safety.stop_running_design(mmio_for, timeout_s=0.02)
        self.assertIn("Reboot", str(cm.exception))

    def test_idle_channels_are_left_alone(self):
        from vidcap import safety
        mmio_for, mm = self._vdma_pair()
        for name in ("vdma_live", "vdma_vmem"):
            m = mmio_for(R.BASES[name])
            m.regs[R.VDMA_S2MM_SR] = m.regs[R.VDMA_MM2S_SR] = R.VDMA_SR_HALTED
        before = len(mm["vdma_live"].writes())
        done = safety.stop_running_design(mmio_for, timeout_s=0.02)
        self.assertTrue(all(d.endswith("idle") for d in done))
        self.assertEqual(len(mm["vdma_live"].writes()), before, "nothing written to a halted DMA")


class TestDiagnose(unittest.TestCase):
    def _s(self, **kw):
        base = dict(ctrl=0x2, status=0b0011, w=1280, h=720, hdmi_frames=1, out_frames=1, mux_frames=1, mux_status=0,
                    s2mm_sr=0x11000, mm2s_sr=0x11000, vmem_sr=0x11000, snap_status=0, snap_trigger=0, snap_ack=0,
                    filter=0, overlay=1, rate_hdmi=60.0, rate_out=60.0, rate_mux=60.0)
        base.update(kw)
        return base

    def test_healthy(self):
        from vidcap import diagnose
        self.assertTrue(diagnose.verdict(self._s())[0].startswith("OK"))

    def test_wedge_signature_is_called_out(self):
        from vidcap import diagnose
        v = diagnose.verdict(self._s(rate_out=0.0, status=0b1011, snap_trigger=3))
        self.assertTrue(v[0].startswith("STALLED"))
        self.assertTrue(any("stuck on the other one" in x or "snapshots were requested" in x for x in v))

    def test_no_signal_and_errors(self):
        from vidcap import diagnose
        v = diagnose.verdict(self._s(status=0, rate_hdmi=0.0, rate_out=0.0, ctrl=0x2))
        self.assertTrue(any("no signal" in x for x in v))
        v = diagnose.verdict(self._s(s2mm_sr=(1 << 7) | 0x11000))
        self.assertTrue(any("SOF early" in x for x in v))

    def test_sample_reads_only(self):
        from vidcap import diagnose
        mm = {}
        def mmio_for(base):
            name = next(n for n, b in R.BASES.items() if b == base)
            mm[name] = FakeMmio(); return mm[name]
        s = diagnose.sample(mmio_for, seconds=0.01)
        self.assertIn("rate_out", s)
        self.assertTrue(all(not m.writes() for m in mm.values()), "diagnose must never write a register")
