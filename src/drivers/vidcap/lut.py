"""Colour look-up tables for the grading block (vc_lut3d): the table format, built-in looks, .cube files.

The hardware holds a 17x17x17 colour cube and interpolates between its nodes (trilinear). In this module a cube is a
uint8 array of shape (17, 17, 17, 3) indexed [r][g][b] -> (R, G, B), node k standing for the input level k*255/16.

    cube = build_look("teal_orange")            one of LOOKS (made here from formulas, no third-party files)
    cube = load_cube("film.cube")               any Adobe/Resolve-style .cube (3D of any size, or 1D), resampled to 17
    out  = apply_lut(rgb_image, cube, 256)      bit-exact model of what the hardware does (previews, tests)
    words = to_bank_words(cube)                 the 8192 words the hardware wants (see docs/register_map.md)

Nothing here needs PYNQ; numpy is the only dependency.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Dict, Tuple, Union

import numpy as np

GRID = 17                       # nodes per axis
NODES = GRID ** 3               # 4913
BANK_WORDS = 1024               # words per bank in the register window (729 used)
STRENGTH_MAX = 256


# ---------------------------------------------------------------------------------------------- table layout
def _identity_levels() -> np.ndarray:
    n = np.arange(GRID)
    return ((n * 255 + 8) // 16).astype(np.uint8)       # same rounding as the hardware's power-up table


def identity_cube() -> np.ndarray:
    lv = _identity_levels()
    r, g, b = np.meshgrid(lv, lv, lv, indexing="ij")
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def to_bank_words(cube: np.ndarray) -> np.ndarray:
    """Cube -> the 8 x 1024 words of the register window, in address order.

    Node (r,g,b) goes to bank {r&1, g&1, b&1} at offset (r>>1) + 9*(g>>1) + 81*(b>>1); the word is 0x00RRGGBB.
    Unused slots are zero.
    """
    cube = _check_cube(cube)
    words = np.zeros(8 * BANK_WORDS, dtype=np.uint32)
    r, g, b = np.meshgrid(np.arange(GRID), np.arange(GRID), np.arange(GRID), indexing="ij")
    bank = ((r & 1) << 2) | ((g & 1) << 1) | (b & 1)
    off = (r >> 1) + 9 * (g >> 1) + 81 * (b >> 1)
    c = cube.astype(np.uint32)
    words[(bank * BANK_WORDS + off).ravel()] = ((c[..., 0] << 16) | (c[..., 1] << 8) | c[..., 2]).ravel()
    return words


def used_slots() -> np.ndarray:
    """Indices (into to_bank_words()) of the 4913 words that hold a node; the other slots of the window are unused."""
    r, g, b = np.meshgrid(np.arange(GRID), np.arange(GRID), np.arange(GRID), indexing="ij")
    idx = (((r & 1) << 2) | ((g & 1) << 1) | (b & 1)) * BANK_WORDS + (r >> 1) + 9 * (g >> 1) + 81 * (b >> 1)
    return np.sort(idx.ravel())


def _check_cube(cube) -> np.ndarray:
    cube = np.asarray(cube)
    if cube.shape != (GRID, GRID, GRID, 3):
        raise ValueError(f"a cube has shape {(GRID, GRID, GRID, 3)}, got {cube.shape}")
    if cube.dtype != np.uint8:
        cube = np.clip(np.rint(cube), 0, 255).astype(np.uint8)
    return cube


def checksum(cube: np.ndarray) -> int:
    """What the hardware's CHECKSUM register holds after this cube has been written once."""
    w = to_bank_words(cube)
    return int(w[used_slots()].astype(np.uint64).sum()) & 0xFFFFFFFF


# ---------------------------------------------------------------------------------------------- bit-exact model
def apply_lut(img: np.ndarray, cube: np.ndarray, strength: int = STRENGTH_MAX) -> np.ndarray:
    """What the hardware does to an (..., 3) uint8 R,G,B image. strength 0..256 (256 = the full look, 0 = unchanged)."""
    cube = _check_cube(cube).astype(np.int64)
    img = np.asarray(img)
    if img.shape[-1] != 3:
        raise ValueError("the last axis must be the R, G, B channels")
    s = int(strength)
    if not 0 <= s <= STRENGTH_MAX:
        raise ValueError(f"strength is 0..{STRENGTH_MAX}")
    px = img.astype(np.int64)
    idx = px >> 4                                             # node index 0..15
    frac = ((px & 15) << 4) | (px >> 4)                       # 8-bit fraction, v=255 -> 255
    ri, gi, bi = idx[..., 0], idx[..., 1], idx[..., 2]
    fr, fg, fb = frac[..., 0:1], frac[..., 1:2], frac[..., 2:3]

    def node(dr, dg, db):
        return cube[ri + dr, gi + dg, bi + db]                # (..., 3)

    def lerp8(a, b, f):
        return a * 256 + (b - a) * f

    def lerp16(a, b, f):
        return (a * 256 + (b - a) * f) >> 8

    t = {(dg, db): lerp8(node(0, dg, db), node(1, dg, db), fr) for dg in (0, 1) for db in (0, 1)}
    u = {db: lerp16(t[(0, db)], t[(1, db)], fg) for db in (0, 1)}
    v = lerp16(u[0], u[1], fb)
    graded = (v + 128) >> 8
    out = px + (((graded - px) * s + 128) >> 8)
    return np.clip(out, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------------------------- built-in looks
def _luma(r, g, b):
    return 0.299 * r + 0.587 * g + 0.114 * b


def _sat(r, g, b, amount):
    y = _luma(r, g, b)
    return y + (r - y) * amount, y + (g - y) * amount, y + (b - y) * amount


def _look_warm(r, g, b):
    return r * 1.06 + 0.02, g * 1.0, b * 0.90


def _look_cool(r, g, b):
    return r * 0.92, g * 1.0, b * 1.08 + 0.02


def _look_sepia(r, g, b):
    y = _luma(r, g, b)
    return y * 1.07 + 0.03, y * 0.94 + 0.01, y * 0.74


def _look_mono(r, g, b):
    y = _luma(r, g, b)
    return y, y, y


def _look_contrast(r, g, b):
    f = lambda x: 0.5 + (x - 0.5) * 1.0 + 0.9 * (x - 0.5) * (1 - 2 * np.abs(x - 0.5)) * 0.5   # noqa: E731
    return f(r), f(g), f(b)


def _look_faded(r, g, b):
    r, g, b = _sat(r, g, b, 0.80)
    f = lambda x: 0.07 + x * 0.84                                                              # noqa: E731
    return f(r) * 1.02, f(g), f(b) * 0.97


def _look_teal_orange(r, g, b):
    y = _luma(r, g, b)
    shadow = np.clip(1 - 2 * y, 0, 1)
    light = np.clip(2 * y - 1, 0, 1)
    mid = 1 - shadow - light
    # shadows lean teal, highlights lean orange, mid-tones get both a little
    r2 = r + 0.10 * light + 0.03 * mid - 0.12 * shadow
    g2 = g + 0.02 * light + 0.02 * shadow
    b2 = b - 0.12 * light - 0.03 * mid + 0.10 * shadow
    return _sat(r2, g2, b2, 1.10)


def _look_bleach(r, g, b):
    y = _luma(r, g, b)
    out = []
    for c in (r, g, b):
        d = 0.45 * y + 0.55 * c                                                                 # keep part of the colour
        out.append(np.where(d < 0.5, 2 * d * d, 1 - 2 * (1 - d) * (1 - d)) * 0.7 + d * 0.3)
    return tuple(out)


def _look_cross(r, g, b):
    return (np.clip(r, 0, 1) ** 1.35 * 0.9 + 0.05 * g,
            np.clip(g, 0, 1) ** 0.85,
            0.15 + 0.7 * b + 0.15 * (1 - r) * b)


def _look_vivid(r, g, b):
    r, g, b = _sat(r, g, b, 1.45)
    f = lambda x: x + 0.12 * (x - 0.5) * (1 - 2 * np.abs(x - 0.5))                              # noqa: E731
    return f(r), f(g), f(b)


def _look_negative(r, g, b):
    return 1 - r, 1 - g, 1 - b


def _look_night_vision(r, g, b):
    y = np.clip(_luma(r, g, b) * 1.3, 0, 1)
    return y * 0.15, y ** 0.8, y * 0.2


LOOKS: Dict[str, Tuple[str, Callable]] = {
    "warm":           ("Warm: a little more red, less blue", _look_warm),
    "cool":           ("Cool: a little more blue, less red", _look_cool),
    "sepia":          ("Sepia: old brown photograph", _look_sepia),
    "mono":           ("Black and white", _look_mono),
    "high_contrast":  ("High contrast: S-shaped curve", _look_contrast),
    "faded_film":     ("Faded film: lifted blacks, softer colour", _look_faded),
    "teal_orange":    ("Teal and orange: cool shadows, warm highlights", _look_teal_orange),
    "bleach_bypass":  ("Bleach bypass: gritty, desaturated, contrasty", _look_bleach),
    "cross_process":  ("Cross process: lab-error colour shifts", _look_cross),
    "vivid":          ("Vivid: stronger colour and punch", _look_vivid),
    "negative":       ("Negative", _look_negative),
    "night_vision":   ("Night vision: green phosphor", _look_night_vision),
}


def look_names():
    return list(LOOKS)


def look_description(name: str) -> str:
    if name == "identity":
        return "No change"
    return LOOKS[name][0]


def build_look(name: str) -> np.ndarray:
    """The cube for a built-in look ("identity" gives the straight-through table)."""
    if name == "identity":
        return identity_cube()
    if name not in LOOKS:
        raise ValueError(f"unknown look {name!r}; choose one of: identity, {', '.join(LOOKS)}")
    x = np.arange(GRID) / (GRID - 1)
    r, g, b = np.meshgrid(x, x, x, indexing="ij")
    out = LOOKS[name][1](r, g, b)
    return np.clip(np.rint(np.stack(out, axis=-1) * 255), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------------------------- .cube files
def parse_cube(text: str) -> np.ndarray:
    """Read the text of a .cube file (3D of any size, or 1D) and return a 17-node cube.

    DOMAIN_MIN / DOMAIN_MAX are honoured. A larger or smaller 3D table is resampled onto the 17 grid with trilinear
    interpolation; a 1D table is applied to each channel independently.
    """
    size3 = size1 = None
    dmin, dmax = np.zeros(3), np.ones(3)
    rows = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key = line.split(None, 1)[0].upper()
        if key == "TITLE":
            continue
        if key == "LUT_3D_SIZE":
            size3 = int(line.split()[1])
        elif key == "LUT_1D_SIZE":
            size1 = int(line.split()[1])
        elif key == "DOMAIN_MIN":
            dmin = np.array([float(v) for v in line.split()[1:4]])
        elif key == "DOMAIN_MAX":
            dmax = np.array([float(v) for v in line.split()[1:4]])
        elif key in ("LUT_3D_INPUT_RANGE", "LUT_1D_INPUT_RANGE"):
            lo, hi = (float(v) for v in line.split()[1:3])
            dmin, dmax = np.full(3, lo), np.full(3, hi)
        elif re.match(r"^[-+0-9.]", line):
            vals = line.split()
            if len(vals) != 3:
                raise ValueError(f"expected 3 numbers per table row, got: {line[:40]!r}")
            rows.append([float(v) for v in vals])
        else:
            raise ValueError(f"unrecognised line in .cube file: {line[:40]!r}")
    if size3 is None and size1 is None:
        raise ValueError("not a .cube file: no LUT_3D_SIZE or LUT_1D_SIZE")
    data = np.array(rows, dtype=np.float64)
    span = np.where(dmax - dmin == 0, 1, dmax - dmin)

    if size3 is not None:
        n = size3
        if n < 2 or len(data) != n ** 3:
            raise ValueError(f"LUT_3D_SIZE {n} needs {n ** 3} rows, found {len(data)}")
        src = data.reshape(n, n, n, 3).transpose(2, 1, 0, 3)          # file order has red varying fastest -> [r][g][b]
        grid = np.arange(GRID) / (GRID - 1) * (n - 1)
        lo = np.minimum(grid.astype(int), n - 2)
        f = grid - lo
        out = src
        for axis in range(3):                                         # resample one axis at a time
            idx0 = np.take(out, lo, axis=axis)
            idx1 = np.take(out, lo + 1, axis=axis)
            shape = [1, 1, 1, 1]
            shape[axis] = GRID
            out = idx0 * (1 - f.reshape(shape)) + idx1 * f.reshape(shape)
        return np.clip(np.rint(out * 255), 0, 255).astype(np.uint8)

    n = size1
    if n < 2 or len(data) != n:
        raise ValueError(f"LUT_1D_SIZE {n} needs {n} rows, found {len(data)}")
    x = np.arange(GRID) / (GRID - 1)
    xs = np.linspace(0, 1, n)
    curves = np.stack([np.interp(x, xs, data[:, c]) for c in range(3)], axis=-1)       # (17, 3)
    cube = np.empty((GRID, GRID, GRID, 3))
    cube[..., 0] = curves[:, 0][:, None, None]
    cube[..., 1] = curves[:, 1][None, :, None]
    cube[..., 2] = curves[:, 2][None, None, :]
    return np.clip(np.rint(cube * 255), 0, 255).astype(np.uint8)


def load_cube(source: Union[str, Path, bytes]) -> np.ndarray:
    """A 17-node cube from a .cube file path, or from the bytes of one (for example an upload from a browser)."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        return parse_cube(bytes(source).decode("utf-8", "replace"))
    return parse_cube(Path(source).read_text(errors="replace"))


def cube_text(cube: np.ndarray, title: str = "vidcap") -> str:
    """The cube as a 17-point .cube file (red varies fastest), for saving a look or opening it in other tools."""
    cube = _check_cube(cube)
    rows = cube.transpose(2, 1, 0, 3).reshape(-1, 3) / 255.0
    head = f'TITLE "{title}"\nLUT_3D_SIZE {GRID}\n'
    return head + "\n".join(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in rows) + "\n"


def resolve(look) -> np.ndarray:
    """A cube from a look name, a .cube file path, .cube bytes, or a ready cube."""
    if isinstance(look, np.ndarray):
        return _check_cube(look)
    if isinstance(look, (bytes, bytearray)):
        return load_cube(look)
    s = str(look)
    if s == "identity" or s in LOOKS:
        return build_look(s)
    if Path(s).is_file():
        return load_cube(s)
    raise ValueError(f"{s!r} is neither a built-in look ({', '.join(look_names())}) nor a .cube file")
