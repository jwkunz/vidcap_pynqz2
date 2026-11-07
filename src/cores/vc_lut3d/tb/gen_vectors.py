#!/usr/bin/env python3
"""gen_vectors.py - Test vectors for tb_vc_lut3d, made by the Python model (src/drivers/vidcap/lut.py).

    gen_vectors.py <output dir>

Writes hex files (one word per line): the register-window image of a random cube (banks_rand.hex), 640 input pixels
(px.hex) and the pixels the hardware must produce for the power-up identity table (exp_ident.hex) and for the random cube
at strength 256 (exp_full.hex) and 100 (exp_half.hex). The testbench compares the RTL against these, so the RTL, the
register-window layout and the Python model agree with each other.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "drivers"))
from vidcap import lut  # noqa: E402

N = 640


def write(path, words, width):
    Path(path).write_text("".join(f"{int(w):0{width}X}\n" for w in words))


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(2026)
    cube = rng.integers(0, 256, (lut.GRID,) * 3 + (3,), dtype=np.uint8)
    px = rng.integers(0, 256, (N, 3), dtype=np.uint8)
    px[:8] = [[0, 0, 0], [255, 255, 255], [255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0], [15, 16, 17], [239, 240, 241]]
    px[8:24, :] = (np.arange(16)[:, None] * 17 + np.array([0, 1, 2])) % 256
    pack = lambda a: (a[..., 0].astype(np.uint32) << 16) | (a[..., 1].astype(np.uint32) << 8) | a[..., 2]   # noqa: E731
    write(out / "banks_rand.hex", lut.to_bank_words(cube), 8)
    write(out / "px.hex", pack(px), 6)
    write(out / "exp_ident.hex", pack(lut.apply_lut(px, lut.identity_cube(), 256)), 6)
    write(out / "exp_full.hex", pack(lut.apply_lut(px, cube, 256)), 6)
    write(out / "exp_half.hex", pack(lut.apply_lut(px, cube, 100)), 6)
    print(f"vectors written to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "vectors")
