"""Tests for the colour-grading side of the driver: table format, built-in looks, .cube files, the ColorLut block."""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from vidcap import ColorLut, FakeMmio, HeapAllocator, Vidcap, VidcapError, lut, regs as R  # noqa: E402


class LutHardware(FakeMmio):
    """Just enough of vc_lut3d for the driver: CTRL/STRENGTH registers and the write counter and checksum."""

    def __init__(self):
        super().__init__({R.LUT_ID: R.LUT_MAGIC, R.LUT_STRENGTH: 256})
        self.writes = self.checksum = 0
        self.cube_words = {}
        self.corrupt = False

    def write(self, offset, value):
        if offset >= R.LUT_CUBE:
            self.writes += 1
            self.checksum = (self.checksum + (value & 0xFFFFFF)) & 0xFFFFFFFF
            self.cube_words[(offset - R.LUT_CUBE) // 4] = value & 0xFFFFFF
            if self.corrupt:
                self.writes -= 1
        elif offset == R.LUT_CTRL:
            if value & R.LUT_CTRL_CLEAR:
                self.writes = self.checksum = 0
            self.regs[R.LUT_CTRL] = value & 1
        else:
            self.regs[offset] = value

    def read(self, offset):
        return {R.LUT_WRITES: self.writes, R.LUT_CHECKSUM: self.checksum}.get(offset, self.regs.get(offset, 0))


class TestTable(unittest.TestCase):
    def setUp(self):
        self.img = np.random.default_rng(7).integers(0, 256, (40, 60, 3), dtype=np.uint8)

    def test_identity_changes_nothing(self):
        self.assertTrue((lut.apply_lut(self.img, lut.identity_cube()) == self.img).all())

    def test_strength_zero_is_exact_and_full_matches_the_look(self):
        cube = lut.build_look("sepia")
        self.assertTrue((lut.apply_lut(self.img, cube, 0) == self.img).all())
        neg = lut.apply_lut(self.img, lut.build_look("negative"))
        self.assertLessEqual(np.abs(neg.astype(int) - (255 - self.img.astype(int))).max(), 1)

    def test_half_strength_is_between(self):
        cube = lut.build_look("negative")
        full, half = lut.apply_lut(self.img, cube, 256).astype(int), lut.apply_lut(self.img, cube, 128).astype(int)
        mid = (self.img.astype(int) + full) / 2
        self.assertLessEqual(np.abs(half - mid).max(), 1.5)

    def test_extremes_and_shapes(self):
        px = np.array([[0, 0, 0], [255, 255, 255], [255, 0, 0]], dtype=np.uint8)
        out = lut.apply_lut(px, lut.identity_cube())
        self.assertTrue((out == px).all())
        self.assertEqual(lut.apply_lut(self.img, lut.identity_cube()).shape, self.img.shape)
        with self.assertRaises(ValueError):
            lut.apply_lut(self.img, lut.identity_cube(), 257)
        with self.assertRaises(ValueError):
            lut.apply_lut(self.img[..., :2], lut.identity_cube())

    def test_bank_layout(self):
        cube = np.random.default_rng(3).integers(0, 256, (17, 17, 17, 3), dtype=np.uint8)
        w = lut.to_bank_words(cube)
        self.assertEqual(w.shape, (8192,))
        for r, g, b in [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (16, 16, 16), (15, 16, 3), (7, 9, 11)]:
            bank = ((r & 1) << 2) | ((g & 1) << 1) | (b & 1)
            off = (r >> 1) + 9 * (g >> 1) + 81 * (b >> 1)
            R_, G_, B_ = cube[r, g, b]
            self.assertEqual(int(w[bank * 1024 + off]), (int(R_) << 16) | (int(G_) << 8) | int(B_), (r, g, b))
        used = lut.used_slots()
        self.assertEqual(len(used), 4913)
        self.assertEqual(len(set(used.tolist())), 4913)
        self.assertEqual(int(used.max()) < 8192, True)
        unused = np.ones(8192, bool)
        unused[used] = False
        self.assertFalse(w[unused].any())
        self.assertEqual(lut.checksum(cube), int(w.astype(np.uint64).sum() & 0xFFFFFFFF))

    def test_cube_must_have_the_right_shape(self):
        with self.assertRaises(ValueError):
            lut.to_bank_words(np.zeros((16, 16, 16, 3), np.uint8))


class TestLooks(unittest.TestCase):
    def test_all_build(self):
        for name in ["identity"] + lut.look_names():
            c = lut.build_look(name)
            self.assertEqual((c.shape, c.dtype), ((17, 17, 17, 3), np.uint8), name)
            self.assertTrue(lut.look_description(name))

    def test_looks_do_what_they_say(self):
        mono = lut.build_look("mono")
        self.assertTrue((mono[..., 0] == mono[..., 1]).all() and (mono[..., 1] == mono[..., 2]).all())
        warm, cool = lut.build_look("warm"), lut.build_look("cool")
        grey = (8, 8, 8)
        self.assertGreater(int(warm[grey][0]), int(warm[grey][2]))
        self.assertGreater(int(cool[grey][2]), int(cool[grey][0]))
        sepia = lut.build_look("sepia")[8, 8, 8]
        self.assertTrue(sepia[0] > sepia[1] > sepia[2])
        faded = lut.build_look("faded_film")
        self.assertGreater(int(faded[0, 0, 0].min()), 10)                  # blacks are lifted
        self.assertLess(int(faded[16, 16, 16].max()), 255)
        night = lut.build_look("night_vision")[12, 12, 12]
        self.assertGreater(int(night[1]), int(night[0]))

    def test_unknown_look(self):
        with self.assertRaises(ValueError):
            lut.build_look("nope")
        with self.assertRaises(ValueError):
            lut.resolve("not-a-look-or-a-file")


class TestCubeFiles(unittest.TestCase):
    @staticmethod
    def identity_text(n, extra=""):
        rows = []
        for b in range(n):
            for g in range(n):
                for r in range(n):
                    rows.append(f"{r / (n - 1):.6f} {g / (n - 1):.6f} {b / (n - 1):.6f}")
        return f'# a comment\nTITLE "test"\n{extra}LUT_3D_SIZE {n}\n' + "\n".join(rows) + "\n"

    def test_identity_files_of_any_size(self):
        for n in (2, 5, 17, 33, 65):
            c = lut.parse_cube(self.identity_text(n))
            self.assertLessEqual(np.abs(c.astype(int) - lut.identity_cube().astype(int)).max(), 1, n)

    def test_red_varies_fastest_in_the_file(self):
        # a 2x2x2 table that outputs (R, G, B) = (input B, input G, input R): a red/blue swap
        rows = []
        for b in (0, 1):
            for g in (0, 1):
                for r in (0, 1):
                    rows.append(f"{b} {g} {r}")
        c = lut.parse_cube("LUT_3D_SIZE 2\n" + "\n".join(rows))
        self.assertEqual(tuple(c[16, 0, 0]), (0, 0, 255))
        self.assertEqual(tuple(c[0, 0, 16]), (255, 0, 0))
        self.assertEqual(tuple(c[0, 16, 0]), (0, 255, 0))

    def test_domain_is_honoured(self):
        rows = [f"{r} {g} {b}" for b in (0, 2) for g in (0, 2) for r in (0, 2)]
        c = lut.parse_cube("DOMAIN_MIN 0 0 0\nDOMAIN_MAX 2 2 2\nLUT_3D_SIZE 2\n" + "\n".join(rows))
        # values 0..2 in a 0..2 domain: not rescaled by us (the file says what the outputs are), so they clip at 1.0
        self.assertEqual(tuple(c[16, 16, 16]), (255, 255, 255))

    def test_one_dimensional_table(self):
        text = "LUT_1D_SIZE 3\n0 0 0\n0.8 0.8 0.8\n1 1 1\n"
        c = lut.parse_cube(text)
        self.assertEqual(tuple(c[8, 8, 8]), (204, 204, 204))
        self.assertEqual(tuple(c[0, 16, 4]), (0, 255, int(c[0, 0, 4][2])))

    def test_bad_files(self):
        for text in ("hello", "LUT_3D_SIZE 2\n0 0 0\n", "LUT_3D_SIZE 2\n" + "1 2\n" * 8, "LUT_3D_SIZE 2\nfoo bar\n"):
            with self.assertRaises(ValueError, msg=text):
                lut.parse_cube(text)

    def test_round_trip_and_bytes(self):
        cube = lut.build_look("teal_orange")
        self.assertTrue((lut.parse_cube(lut.cube_text(cube)) == cube).all())
        self.assertTrue((lut.load_cube(lut.cube_text(cube).encode()) == cube).all())
        self.assertTrue((lut.resolve(lut.cube_text(cube).encode()) == cube).all())

    def test_file_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.cube")
            with open(p, "w") as f:
                f.write(lut.cube_text(lut.build_look("vivid")))
            self.assertTrue((lut.resolve(p) == lut.build_look("vivid")).all())


class TestColorLutBlock(unittest.TestCase):
    def test_load_writes_every_node_and_verifies(self):
        hw = LutHardware()
        cube = lut.build_look("warm")
        ColorLut(hw).load(cube)
        self.assertEqual(hw.writes, 4913)
        self.assertEqual(hw.checksum, lut.checksum(cube))
        for i in lut.used_slots()[:50]:
            self.assertEqual(hw.cube_words[int(i)], int(lut.to_bank_words(cube)[i]))

    def test_failed_load_is_reported(self):
        hw = LutHardware()
        hw.corrupt = True
        with self.assertRaises(VidcapError):
            ColorLut(hw).load(lut.identity_cube())

    def test_load_keeps_enable(self):
        hw = LutHardware()
        b = ColorLut(hw)
        b.enable(True)
        b.load(lut.identity_cube())
        self.assertTrue(b.enabled())

    def test_controls(self):
        hw = LutHardware()
        b = ColorLut(hw)
        b.check()
        b.set_strength(100)
        self.assertEqual(b.strength(), 100)
        for bad in (-1, 257):
            with self.assertRaises(ValueError):
                b.set_strength(bad)
        hw.regs[R.LUT_PROBE_IN] = 0x102030
        hw.regs[R.LUT_PROBE_OUT] = 0xA0B0C0
        self.assertEqual(b.probe(), ((0x10, 0x20, 0x30), (0xA0, 0xB0, 0xC0)))

    def test_wrong_bitstream(self):
        with self.assertRaises(VidcapError):
            ColorLut(FakeMmio()).check()


def _vidcap_with_lut():
    from test_vidcap import _fake_pipeline
    v, mm = _fake_pipeline()
    hw = LutHardware()
    v.lut = ColorLut(hw)
    return v, hw


class TestSetLook(unittest.TestCase):
    def test_builtin_look(self):
        v, hw = _vidcap_with_lut()
        v.set_look("sepia", strength=50)
        self.assertTrue(v.lut.enabled())
        self.assertEqual(v.lut.strength(), 128)
        self.assertEqual(v.look, "sepia")
        self.assertEqual(hw.checksum, lut.checksum(lut.build_look("sepia")))
        self.assertEqual(v.status()["color_grade"], {"look": "sepia", "enabled": True, "strength_percent": 50})

    def test_default_is_the_full_look_and_off_switches_it_off(self):
        v, hw = _vidcap_with_lut()
        v.set_look("warm")
        self.assertEqual(v.lut.strength(), 256)
        v.set_look(None)
        self.assertFalse(v.lut.enabled())
        self.assertIsNone(v.look)

    def test_custom_cube_and_changing_look(self):
        v, hw = _vidcap_with_lut()
        v.set_look("warm")
        v.set_look(lut.cube_text(lut.build_look("cool")).encode())
        self.assertEqual(v.look, "custom")
        self.assertEqual(hw.checksum, lut.checksum(lut.build_look("cool")))
        self.assertTrue(v.lut.enabled())

    def test_table_is_loaded_with_grading_off(self):
        v, hw = _vidcap_with_lut()
        v.set_look("warm")
        order = []
        orig = hw.write

        def spy(offset, value):
            if offset == R.LUT_CTRL or offset == R.LUT_CUBE:
                order.append((offset, value))
            orig(offset, value)
        hw.write = spy
        v.set_look("cool")
        self.assertEqual(order[0], (R.LUT_CTRL, 0), "switched off before the first table word")
        self.assertEqual(order[-1][0], R.LUT_CTRL)
        self.assertEqual(order[-1][1] & 1, 1, "switched on after the last one")

    def test_strength_only_change(self):
        v, hw = _vidcap_with_lut()
        v.set_look("warm")
        before = hw.writes
        v.set_look_strength(25)
        self.assertEqual(v.lut.strength(), 64)
        self.assertEqual(hw.writes, before)
        for bad in (-1, 101):
            with self.assertRaises(ValueError):
                v.set_look_strength(bad)

    def test_old_bitstream_is_never_touched(self):
        v, hw = _vidcap_with_lut()
        v.has_lut = False
        with self.assertRaises(VidcapError):
            v.set_look("warm")
        self.assertNotIn("color_grade", v.status())
        v.start()                                  # start() must not read the missing block either
        self.assertEqual(hw.writes, 0)

    def test_looks_listing(self):
        d = Vidcap.looks()
        self.assertIn("identity", d)
        self.assertEqual(set(d) - {"identity"}, set(lut.look_names()))


try:
    import ipywidgets  # noqa: F401
    HAVE_WIDGETS = True
except ImportError:
    HAVE_WIDGETS = False


@unittest.skipUnless(HAVE_WIDGETS, "ipywidgets not installed")
class TestPanel(unittest.TestCase):
    def setUp(self):
        from vidcap import ui
        self.v, self.hw = _vidcap_with_lut()
        self.v.start()
        self.panel = ui.build_panel(self.v, refresh_s=0.05)

    def tearDown(self):
        self.panel.stop()

    def test_picking_a_look(self):
        w = self.panel.widgets
        w["look"].value = "teal_orange"
        self.assertEqual(self.v.look, "teal_orange")
        self.assertTrue(self.v.lut.enabled())
        w["look_strength"].value = 40
        self.assertEqual(self.v.lut.strength(), round(40 * 256 / 100))
        w["look"].value = "off"
        self.assertFalse(self.v.lut.enabled())
        self.assertEqual(self.panel.messages, [])

    def test_uploading_a_cube(self):
        w = self.panel.widgets
        data = lut.cube_text(lut.build_look("sepia")).encode()
        w["look_upload"].value = ({"name": "my.cube", "type": "", "size": len(data), "content": memoryview(data),
                                   "last_modified": __import__("datetime").datetime(2026, 1, 1)},)
        self.assertEqual(w["look"].value, "custom")
        self.assertEqual(self.v.look, "custom")
        self.assertEqual(self.hw.checksum, lut.checksum(lut.build_look("sepia")))
        self.assertIn("my.cube", w["look_info"].value)

    def test_a_bad_file_is_reported_not_raised(self):
        w = self.panel.widgets
        data = b"this is not a cube"
        w["look_upload"].value = ({"name": "bad.cube", "type": "", "size": len(data), "content": memoryview(data),
                                   "last_modified": __import__("datetime").datetime(2026, 1, 1)},)
        self.assertTrue(self.panel.messages)
        self.assertFalse(self.v.lut.enabled())

    def test_old_bitstream_disables_the_controls(self):
        from vidcap import ui
        self.v.has_lut = False
        panel = ui.build_panel(self.v, refresh_s=0.05)
        try:
            self.assertTrue(panel.widgets["look"].disabled)
            self.assertIn("no colour grading", panel.widgets["look_info"].value)
        finally:
            panel.stop()


class TestNotebooks(unittest.TestCase):
    NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")

    def _load(self, name):
        import json
        with open(os.path.join(self.NB, name)) as fh:
            return json.load(fh)

    def test_demo_notebook_grading_cells_run(self):
        """The colour-grading cells of the demo notebook run against the fake hardware."""
        import time as _time
        nb = self._load("vidcap_demo.ipynb")
        start = next(i for i, c in enumerate(nb["cells"]) if "".join(c["source"]).startswith("## Colour grading"))
        end = next(i for i, c in enumerate(nb["cells"]) if "".join(c["source"]).startswith("## Test pattern"))
        v, hw = _vidcap_with_lut()
        v.start()
        v.snapshot = lambda: np.random.default_rng(1).integers(0, 256, v.geo.shape, dtype=np.uint8)
        saved_sleep, saved_cwd = _time.sleep, os.getcwd()
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _time.sleep = lambda s: None
            try:
                g = {"v": v, "time": _time}
                import io, contextlib
                with contextlib.redirect_stdout(io.StringIO()):
                    for c in nb["cells"][start:end]:
                        if c["cell_type"] == "code":
                            exec("".join(c["source"]), g)
                self.assertTrue(os.path.exists("warm.cube"))
                with open("warm.cube") as fh:
                    self.assertTrue((lut.parse_cube(fh.read()) == lut.build_look("warm")).all())
            finally:
                _time.sleep = saved_sleep
                os.chdir(saved_cwd)
        self.assertFalse(v.lut.enabled(), "the demo ends with grading off")

    def test_both_notebooks_mention_grading_and_every_mode(self):
        for name in ("vidcap_demo.ipynb", "vidcap_interactive.ipynb"):
            text = "".join("".join(c["source"]) for c in self._load(name)["cells"])
            self.assertIn("rading", text, name)
            for mode in R.MODES:
                self.assertIn(f"'{mode}'", text, f"{name} should list the {mode} mode")

    def test_interactive_notebook_builds_the_panel_with_look_controls(self):
        text = "".join("".join(c["source"]) for c in self._load("vidcap_interactive.ipynb")["cells"])
        self.assertIn("ui.build_panel", text)
        self.assertIn("Upload .cube", text)


if __name__ == "__main__":
    unittest.main()
