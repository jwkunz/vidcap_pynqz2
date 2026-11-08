"""Tests for the video modes: the mode files, the EDIDs generated from them, and the driver's geometry table."""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src", "overlay", "edid"))

import numpy as np                                            # noqa: E402

import gen_edid                                               # noqa: E402
from vidcap import FakeMmio, HeapAllocator, Vidcap, VidcapError, regs as R   # noqa: E402

MODES_DIR = os.path.join(ROOT, "src", "overlay", "modes")


def load(mode):
    with open(os.path.join(MODES_DIR, f"{mode}.env")) as fh:
        return dict(re.findall(r"^([A-Z_0-9]+)=(\S+)", fh.read(), re.M))


class TestModeFiles(unittest.TestCase):
    def test_every_mode_file_has_a_driver_geometry_and_agrees_with_it(self):
        files = sorted(f[:-4] for f in os.listdir(MODES_DIR) if f.endswith(".env"))
        self.assertEqual(files, sorted(R.MODES), "a mode exists in the build but not in the driver, or the reverse")
        for name in files:
            d, g = load(name), R.MODES[name]
            self.assertEqual((int(d["WIDTH"]), int(d["HEIGHT"]), float(d["PIXEL_MHZ"])), (g.width, g.height, g.pixel_mhz), name)
            self.assertEqual(d["MODE"], name)

    def test_widths_are_multiples_of_eight(self):
        for name, g in R.MODES.items():                        # the snapshot and temporal-average cores pack 8 pixels per word
            self.assertEqual(g.width % 8, 0, name)

    def test_the_clock_bands_keep_the_mmcm_in_range(self):
        # The Digilent IP multiplies the pixel clock by 5k, so its VCO runs at 5*k*pixel clock; the Zynq-7000 MMCM
        # (-1 speed grade) needs 600..1200 MHz. The IP also documents the lowest pixel clock each band k is for.
        floor = {1: 120, 2: 60, 3: 40, 4: 30, 5: 25}
        for name in R.MODES:
            d = load(name)
            for key in ("RX_CLK_RANGE", "TX_CLK_RANGE"):
                k, clk = int(d[key]), float(d["PIXEL_MHZ"])
                self.assertTrue(600 <= 5 * k * clk <= 1200, f"{name} {key}={k}: VCO {5 * k * clk} MHz")
                self.assertGreaterEqual(clk, floor[k] - 1, f"{name} {key}={k} is for >= {floor[k]} MHz")

    def test_no_single_band_covers_both_clock_families(self):
        # why there is one bitstream per mode family: 27 MHz and 74.25 MHz cannot share a band
        bands = lambda clk: {k for k in range(1, 6) if 600 <= 5 * k * clk <= 1200}     # noqa: E731
        self.assertEqual(bands(27.0) & bands(74.25), set())

    def test_custom_timing_is_consistent_and_runs_at_about_60_hz(self):
        for name in R.MODES:
            d = load(name)
            if d["VTC_MODE"] != "Custom":
                continue
            w, h = int(d["WIDTH"]), int(d["HEIGHT"])
            self.assertTrue(w < int(d["H_SYNC_START"]) < int(d["H_SYNC_END"]) < int(d["H_FRAME"]), name)
            self.assertTrue(h < int(d["V_SYNC_START"]) < int(d["V_SYNC_END"]) < int(d["V_FRAME"]), name)
            hz = float(d["PIXEL_MHZ"]) * 1e6 / (int(d["H_FRAME"]) * int(d["V_FRAME"]))
            want = {"1080p30": 30.0}.get(name, 60.0)
            self.assertTrue(want - 0.5 < hz < want + 0.5, f"{name}: {hz:.2f} Hz")

    def test_known_cea_timings(self):
        d = load("480p")        # CEA-861 VIC 2/3: 858 x 525 total, sync 62 / 6
        self.assertEqual((d["H_FRAME"], d["V_FRAME"], int(d["H_SYNC_END"]) - int(d["H_SYNC_START"]),
                          int(d["V_SYNC_END"]) - int(d["V_SYNC_START"])), ("858", "525", 62, 6))
        d = load("1080p30")     # CEA-861 VIC 34: 2200 x 1125 total, sync 44 / 5, positive
        self.assertEqual((d["H_FRAME"], d["V_FRAME"], int(d["H_SYNC_END"]) - int(d["H_SYNC_START"]),
                          int(d["V_SYNC_END"]) - int(d["V_SYNC_START"]), d["SYNC_POLARITY"]), ("2200", "1125", 44, 5, "High"))
        d = load("xga")         # VESA DMT 0x10: 1344 x 806 total, sync 136 / 6, negative
        self.assertEqual((d["H_FRAME"], d["V_FRAME"], int(d["H_SYNC_END"]) - int(d["H_SYNC_START"]),
                          int(d["V_SYNC_END"]) - int(d["V_SYNC_START"]), d["SYNC_POLARITY"]), ("1344", "806", 136, 6, "Low"))
        d = load("240p")        # CEA-861 VIC 8/9 with pixel repetition: 1716 x 262 total, horizontal values doubled
        self.assertEqual((d["H_FRAME"], d["V_FRAME"], int(d["H_SYNC_END"]) - int(d["H_SYNC_START"]),
                          int(d["V_SYNC_END"]) - int(d["V_SYNC_START"])), ("1716", "262", 124, 3))


class TestEdid(unittest.TestCase):
    def _decode(self, data):
        w = data[56] | ((data[58] >> 4) << 8)
        hb = data[57] | ((data[58] & 15) << 8)
        h = data[59] | ((data[61] >> 4) << 8)
        vb = data[60] | ((data[61] & 15) << 8)
        clk = (data[55] << 8 | data[54]) * 10_000
        hfp = data[62] | (((data[65] >> 6) & 3) << 8)
        hsw = data[63] | (((data[65] >> 4) & 3) << 8)
        vfp = (data[64] >> 4) | (((data[65] >> 2) & 3) << 4)
        vsw = (data[64] & 15) | ((data[65] & 3) << 4)
        return dict(w=w, hb=hb, h=h, vb=vb, clk=clk, hfp=hfp, hsw=hsw, vfp=vfp, vsw=vsw)

    def test_generated_edids_are_valid_and_describe_their_mode(self):
        for name in ("480p", "240p", "1080p30", "xga"):
            data, _ = gen_edid.build(name)
            self.assertEqual(len(data), 128)
            self.assertEqual(data[:8], bytes([0, 255, 255, 255, 255, 255, 255, 0]))
            self.assertEqual(sum(data) % 256, 0, "checksum")
            d, m = self._decode(data), load(name)
            self.assertEqual((d["w"], d["h"]), (int(m["WIDTH"]), int(m["HEIGHT"])))
            self.assertEqual(d["w"] + d["hb"], int(m["H_FRAME"]))
            self.assertEqual(d["h"] + d["vb"], int(m["V_FRAME"]))
            self.assertEqual(d["clk"], round(float(m["PIXEL_MHZ"]) * 1e6))
            self.assertEqual(d["hfp"], int(m["H_SYNC_START"]) - int(m["WIDTH"]))
            self.assertEqual(d["hsw"], int(m["H_SYNC_END"]) - int(m["H_SYNC_START"]))
            self.assertEqual(d["vsw"], int(m["V_SYNC_END"]) - int(m["V_SYNC_START"]))
            self.assertEqual(data[17 + 54] & 0x06, 0x06 if m["SYNC_POLARITY"] == "High" else 0, "sync polarity")
            hz = d["clk"] / ((d["w"] + d["hb"]) * (d["h"] + d["vb"]))
            self.assertTrue(data[90 + 5] <= hz <= data[90 + 6], f"{name}: vertical rate limits {data[95]}..{data[96]} Hz must include {hz:.1f}")

    def test_file_format_matches_the_stock_one(self):
        stock = os.path.join(ROOT, "workspace", "vivado", "refs", "PYNQ", "boards", "ip", "dvi2rgb_v1_7", "src", "720p_edid.data")
        if not os.path.exists(stock):
            self.skipTest("reference IP not fetched")
        with open(stock) as fh:
            text = fh.read().split()
        self.assertEqual((len(text), {len(t) for t in text}), (128, {8}), "128 lines of 8 binary digits")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "x.data")
            gen_edid.write(out, gen_edid.build("480p")[0])
            with open(out) as fh:
                mine = fh.read().split()
            self.assertEqual((len(mine), {len(t) for t in mine}), (128, {8}))
        d = self._decode(bytes(int(t, 2) for t in text))
        self.assertEqual((d["w"], d["h"], d["clk"]), (1280, 720, 74_250_000), "the decoder reads a known-good EDID correctly")


class TestDriverModes(unittest.TestCase):
    def test_default_history_fits_the_memory_pool(self):
        from vidcap.pipeline import default_history_frames
        from vidcap.blocks import TemporalAverage
        for name, g in R.MODES.items():
            n = default_history_frames(g)
            self.assertEqual(n, 16 if g.frame_bytes <= 3_000_000 else 8, name)
            total = 6 * g.frame_bytes + TemporalAverage.bytes_needed(n, g)
            self.assertLess(total, 68_000_000 - 8_000_000, f"{name}: {total / 1e6:.1f} MB must leave room in the ~68 MB free after boot")

    def test_test_pattern_is_paced_at_the_modes_frame_rate(self):
        v, mm = self._pipeline("1080p30")
        v.start(sync_time=False)
        self.assertEqual(mm["isel"].regs[R.ISEL_TPG_PERIOD], int(R.ACLK_HZ / 30.0))
        v, mm = self._pipeline("720p")
        v.start(sync_time=False)
        self.assertNotIn(R.ISEL_TPG_PERIOD, mm["isel"].regs, "60 Hz modes keep the hardware default")

    def test_start_uses_the_default_history_of_the_mode(self):
        v, _ = self._pipeline("1080p30")
        v.start(sync_time=False)
        self.assertEqual(v.history_frames, 8)

    def _pipeline(self, mode):
        ids = {"rtc": {R.RTC_ID: R.RTC_MAGIC}, "isel": {R.ISEL_ID: R.ISEL_MAGIC}, "vmux": {R.VMUX_ID: R.VMUX_MAGIC}, "lut": {R.LUT_ID: R.LUT_MAGIC}}
        mm = {}

        def mmio_for(base):
            name = next(n for n, b in R.BASES.items() if b == base)
            m = FakeMmio(ids.get(name))
            if name.startswith("vdma"):
                for cr in (R.VDMA_S2MM_CR, R.VDMA_MM2S_CR):
                    m.on_write[cr] = lambda v, mm_, cr=cr: mm_.regs.__setitem__(cr, v & ~R.VDMA_CR_RESET)
            mm[name] = m
            return m
        return Vidcap(mmio_for, HeapAllocator(), mode=mode), mm

    def test_geometry_values(self):
        g = R.MODES["240p"]
        self.assertEqual((g.shape, g.line_bytes, g.frame_bytes, g.bitfile), ((240, 1440, 3), 4320, 1_036_800, "vidcap_240p.bit"))
        self.assertEqual(R.MODES["480p"].frame_bytes, g.frame_bytes, "720x480 and 1440x240 have the same number of pixels")
        self.assertEqual((R.WIDTH, R.HEIGHT), (1280, 720), "module constants stay the 720p defaults")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            Vidcap(lambda b: FakeMmio(), HeapAllocator(), mode="1080p")

    def test_start_sizes_everything_for_the_mode(self):
        for name in ("480p", "240p"):
            v, mm = self._pipeline(name)
            g = R.MODES[name]
            v.start(sync_time=False, history_frames=6)
            self.assertEqual(v._live[0].array.shape, g.shape)
            self.assertEqual(v._snapbuf.array.shape, g.shape)
            self.assertEqual(len(v._tavgbuf.array), 6 * g.luma_frame_bytes + g.sum_frame_bytes)
            live = mm["vdma_live"].regs
            self.assertEqual((live[R.VDMA_S2MM_HSIZE], live[R.VDMA_S2MM_VSIZE], live[R.VDMA_S2MM_STRIDE]),
                             (g.line_bytes, g.height, g.line_bytes), name)
            self.assertEqual((live[R.VDMA_MM2S_HSIZE], live[R.VDMA_MM2S_VSIZE]), (g.line_bytes, g.height), name)
            vm = mm["vdma_vmem"].regs
            self.assertEqual((vm[R.VDMA_MM2S_HSIZE], vm[R.VDMA_MM2S_VSIZE]), (g.line_bytes, g.height), name)
            self.assertEqual(v.status()["mode"], name)

    def test_images_must_match_the_mode(self):
        v, mm = self._pipeline("240p")
        v.start(sync_time=False, history_frames=2)
        with self.assertRaises(ValueError) as cm:
            v.show_image(np.zeros((720, 1280, 3), np.uint8))
        self.assertIn("240p", str(cm.exception))
        v.show_image(np.zeros((240, 1440, 3), np.uint8))
        self.assertEqual(mm["vmux"].regs[R.VMUX_SEL], 1)

    def test_bitfile_lookup(self):
        g = R.MODES["480p"]
        with tempfile.TemporaryDirectory() as tmp:
            old = os.getcwd()
            os.chdir(tmp)
            try:
                with self.assertRaises(VidcapError) as cm:
                    Vidcap.find_bitfile(g)
                self.assertIn("vidcap_480p.bit", str(cm.exception))
                open(os.path.join(tmp, "vidcap_480p.bit"), "w").close()
                self.assertTrue(Vidcap.find_bitfile(g).endswith("vidcap_480p.bit"))
            finally:
                os.chdir(old)

    def test_a_design_of_another_mode_counts_as_loaded(self):
        from vidcap import safety
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        m = os.path.getmtime(f.name)
        for loaded in ("/n/vidcap_720p.bit", "/n/vidcap_240p.bit", "/n/vidcap.bit"):
            self.assertTrue(safety.design_loaded_since_boot("/n/vidcap_480p.bit", f.name, m - 10, loaded), loaded)
        self.assertFalse(safety.design_loaded_since_boot("/n/vidcap_480p.bit", f.name, m - 10, "/n/base.bit"))
        os.unlink(f.name)

    def test_panel_images_follow_the_mode(self):
        from vidcap import ui
        for name, g in R.MODES.items():
            for make in (ui.image_gradient, ui.image_bars, ui.image_checker):
                self.assertEqual(make(g).shape, g.shape, f"{name} {make.__name__}")
        bars = ui.image_bars(R.MODES["240p"])
        self.assertEqual(tuple(bars[0, 5 * (1440 // 8) + 1]), (0, 0, 255), "red bar is still bar 6, in B,G,R order")
        self.assertEqual(len(ui.png_bytes(bars, width=240)) > 100, True)


if __name__ == "__main__":
    unittest.main()
