#!/usr/bin/env python3
"""prepare_ip.py - Make a patched local copy of the Digilent HDMI IP in workspace/vivado/ip_local.

Why a patched copy: the stock IP definitions only offer two receiver/transmitter clock bands (>=120 MHz, <120 MHz)
and four fixed EDID files, although the VHDL supports five bands and any EDID file. The non-720p builds need
the 24..48 MHz band (band 5) and their own EDIDs. The original files under workspace/vivado/refs stay untouched.

    ip_local/dvi2rgb_v1_7    receiver: + clock bands 3..5, + 480p_edid.data and 240p_edid.data (generated here)
    ip_local/rgb2dvi_v1_2    transmitter: + clock bands 3..5
    ip_local/if              interface definitions (copied unchanged)

Band k means the pixel clock is >= 120, 60, 40, 30, 25 MHz for k = 1..5; the IP multiplies by 5k so the MMCM VCO
(5 x k x pixel clock) stays within 600..1200 MHz. Safe to run repeatedly.
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REFS_IP = ROOT / "workspace" / "vivado" / "refs" / "PYNQ" / "boards" / "ip"
LOCAL = ROOT / "workspace" / "vivado" / "ip_local"
sys.path.insert(0, str(ROOT / "src" / "overlay" / "edid"))
import gen_edid  # noqa: E402

BANDS = [(3, ">=40 MHz"), (4, ">=30 MHz"), (5, ">=25 MHz (480p, 240p)")]


def add_after(text, anchor, new, what):
    if anchor not in text:
        sys.exit(f"cannot patch {what}: anchor not found")
    return text.replace(anchor, anchor + new, 1)


def patch_bands(xml, choice_name, what):
    m = re.search(r"(<spirit:name>%s</spirit:name>.*?)(</spirit:choice>)" % re.escape(choice_name), xml, re.S)
    if not m:
        sys.exit(f"cannot patch {what}: choice {choice_name} not found")
    body = m.group(1)
    if 'spirit:text=">=25 MHz' in body:
        return xml
    extra = "".join(f'      <spirit:enumeration spirit:text="{t}">{k}</spirit:enumeration>\n' for k, t in BANDS)
    return xml[:m.end(1)] + extra + "    " + xml[m.start(2):]


def main():
    if not REFS_IP.exists():
        sys.exit(f"{REFS_IP} is missing: run src/overlay/scripts/fetch_refs.sh first")
    LOCAL.mkdir(parents=True, exist_ok=True)
    for name in ("dvi2rgb_v1_7", "rgb2dvi_v1_2", "if"):
        dst = LOCAL / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(REFS_IP / name, dst)

    # ---- receiver
    rx = LOCAL / "dvi2rgb_v1_7"
    edids = ["480p", "240p", "1080p30", "xga"]
    for m in edids:
        data, _ = gen_edid.build(m)
        gen_edid.write(rx / "src" / f"{m}_edid.data", data)
    xml_path = rx / "component.xml"
    xml = xml_path.read_text()
    xml = patch_bands(xml, "choice_pairs_376da672", "receiver clock bands")
    for m in edids:
        w, h = gen_edid.load_mode(m)["WIDTH"], gen_edid.load_mode(m)["HEIGHT"]
        xml = add_after(xml, '<spirit:enumeration spirit:text="1280x720">720p_edid.data</spirit:enumeration>\n',
                        f'      <spirit:enumeration spirit:text="{w}x{h}">{m}_edid.data</spirit:enumeration>\n', "EDID choices")
        entry = (f"      <spirit:file>\n        <spirit:name>src/{m}_edid.data</spirit:name>\n"
                 "        <spirit:userFileType>data</spirit:userFileType>\n        <spirit:userFileType>mif</spirit:userFileType>\n"
                 "      </spirit:file>\n")
        xml = add_after(xml, "      <spirit:file>\n        <spirit:name>src/720p_edid.data</spirit:name>\n"
                             "        <spirit:userFileType>data</spirit:userFileType>\n        <spirit:userFileType>mif</spirit:userFileType>\n"
                             "      </spirit:file>\n", entry, "EDID files")
    xml_path.write_text(xml)

    # ---- transmitter
    tx = LOCAL / "rgb2dvi_v1_2" / "component.xml"
    tx.write_text(patch_bands(tx.read_text(), "choice_pairs_c59fad91", "transmitter clock bands"))

    print(f"patched IP written to {LOCAL}")
    for m in edids:
        print("  " + gen_edid.check(rx / "src" / f"{m}_edid.data"))


if __name__ == "__main__":
    main()
