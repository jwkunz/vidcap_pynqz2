#!/usr/bin/env python3
"""gen_edid.py - Generate the EDID files the HDMI receiver shows to the source, one per video mode.

The Digilent dvi2rgb receiver emulates a 128-byte EDID EEPROM, loaded from a text file with one byte per line
written as 8 binary digits. The stock files cover 720p and larger, so this writes 480p and 240p ones from the
timing in src/overlay/modes/<mode>.env. Each is a plain EDID 1.3 base block with one detailed timing (the mode),
a name, and range limits that allow only that mode.

    gen_edid.py <mode> <output.data>        write one file
    gen_edid.py --check <file.data>         validate a file (header, checksum) and print what it advertises
"""
import re
import sys
from pathlib import Path

MODES_DIR = Path(__file__).resolve().parent.parent / "modes"


def load_mode(name):
    text = (MODES_DIR / f"{name}.env").read_text()
    return {k: v for k, v in re.findall(r"^([A-Z_0-9]+)=(\S+)", text, re.M)}


def manufacturer(code):
    a, b, c = (ord(ch) - ord("A") + 1 for ch in code)
    v = (a << 10) | (b << 5) | c
    return bytes([v >> 8, v & 0xFF])


def dtd(m):
    """Detailed timing descriptor (18 bytes) for the mode."""
    w, h = int(m["WIDTH"]), int(m["HEIGHT"])
    hf, vf = int(m["H_FRAME"]), int(m["V_FRAME"])
    hblank, vblank = hf - w, vf - h
    hfp = int(m["H_SYNC_START"]) - w
    hsw = int(m["H_SYNC_END"]) - int(m["H_SYNC_START"])
    vfp = int(m["V_SYNC_START"]) - h
    vsw = int(m["V_SYNC_END"]) - int(m["V_SYNC_START"])
    clk = round(float(m["PIXEL_MHZ"]) * 100)               # units of 10 kHz
    # image size in mm: 4:3 picture, 14 inch diagonal (informative only)
    himg, vimg = 285, 214
    b = bytearray(18)
    b[0], b[1] = clk & 0xFF, clk >> 8
    b[2], b[3], b[4] = w & 0xFF, hblank & 0xFF, ((w >> 8) << 4) | (hblank >> 8)
    b[5], b[6], b[7] = h & 0xFF, vblank & 0xFF, ((h >> 8) << 4) | (vblank >> 8)
    b[8], b[9] = hfp & 0xFF, hsw & 0xFF
    b[10] = ((vfp & 0xF) << 4) | (vsw & 0xF)
    b[11] = ((hfp >> 8) << 6) | ((hsw >> 8) << 4) | ((vfp >> 4) << 2) | (vsw >> 4)
    b[12], b[13], b[14] = himg & 0xFF, vimg & 0xFF, ((himg >> 8) << 4) | (vimg >> 8)
    pos = 0x06 if m.get("SYNC_POLARITY", "Low") == "High" else 0x00     # bit2 vsync+, bit1 hsync+
    b[17] = 0x18 | pos                                      # digital separate sync
    return bytes(b)


def text_descriptor(tag, text):
    return bytes([0, 0, 0, tag, 0]) + (text[:12] + "\n").ljust(13).encode("ascii")


def build(name):
    m = load_mode(name)
    hz = float(m["PIXEL_MHZ"]) * 1e6 / (int(m["H_FRAME"]) * int(m["V_FRAME"]))
    hkhz = float(m["PIXEL_MHZ"]) * 1e3 / int(m["H_FRAME"])
    e = bytearray(128)
    e[0:8] = bytes([0, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0])
    e[8:10] = manufacturer("VCP")
    e[10:12] = bytes([int(m["WIDTH"]) & 0xFF, 0x01])         # product code
    e[12:16] = bytes([1, 0, 0, 0])
    e[16], e[17] = 1, 2026 - 1990
    e[18], e[19] = 1, 3                                      # EDID 1.3
    e[20] = 0x80                                             # digital input
    e[21], e[22] = 29, 22                                    # cm, informative
    e[23] = 0x78                                             # gamma 2.2
    e[24] = 0x0A                                             # RGB, preferred timing is the first descriptor
    e[25:35] = bytes([0xEE, 0x95, 0xA3, 0x54, 0x4C, 0x99, 0x26, 0x0F, 0x50, 0x54])   # sRGB primaries
    e[35:38] = bytes([0, 0, 0])                              # no established timings
    for i in range(8):                                       # standard timings: unused
        e[38 + 2 * i:40 + 2 * i] = bytes([0x01, 0x01])
    e[54:72] = dtd(m)
    e[72:90] = text_descriptor(0xFC, f"vidcap {name}")
    vmin, vmax = (50, 70) if hz >= 50 else (max(1, int(hz) - 5), int(hz) + 10)      # vertical rate limits must include the mode
    rng = bytearray([0, 0, 0, 0xFD, 0, vmin, vmax, max(1, int(hkhz) - 1), int(hkhz) + 2, int(float(m["PIXEL_MHZ"]) / 10) + 1, 0, 0x0A])
    rng += bytes([0x20] * 6)
    e[90:108] = rng[:18]
    e[108:126] = text_descriptor(0xFF, "00000001")
    e[126] = 0
    e[127] = (-sum(e[:127])) & 0xFF
    return bytes(e), hz


def write(path, data):
    Path(path).write_text("\n".join(format(b, "08b") for b in data) + "\n")


def check(path):
    data = bytes(int(line, 2) for line in Path(path).read_text().split())
    assert len(data) == 128, f"{len(data)} bytes, expected 128"
    assert data[:8] == bytes([0, 255, 255, 255, 255, 255, 255, 0]), "bad header"
    assert sum(data) % 256 == 0, "bad checksum"
    clk = (data[55] << 8 | data[54]) * 10_000
    w = data[56] | ((data[58] >> 4) << 8)
    hb = data[57] | ((data[58] & 15) << 8)
    h = data[59] | ((data[61] >> 4) << 8)
    vb = data[60] | ((data[61] & 15) << 8)
    name = bytes(data[77:90]).decode("ascii", "replace").strip()
    hz = clk / ((w + hb) * (h + vb)) if clk else 0
    return f"{path}: valid EDID, preferred timing {w}x{h} @ {hz:.2f} Hz, pixel clock {clk / 1e6:.3f} MHz, name '{name}'"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        print(check(sys.argv[2]))
    elif len(sys.argv) == 3:
        data, hz = build(sys.argv[1])
        write(sys.argv[2], data)
        print(check(sys.argv[2]))
    else:
        sys.exit(__doc__)
