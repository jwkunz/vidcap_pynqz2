"""Interactive control panel for Jupyter (ipywidgets). Every control calls the same driver methods the
non-interactive notebook uses; the panel adds nothing to the hardware contract.

    from vidcap import Vidcap, ui
    v = Vidcap.on_pynq('vidcap.bit'); v.start()
    panel = ui.build_panel(v); display(panel)
"""
from __future__ import annotations

import io
import os
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

import numpy as np

from . import regs as R
from .blocks import VidcapError

SNAPSHOT_DIR = "/home/xilinx/jupyter_notebooks/vidcap/snapshots"


# ------------------------------------------------------------------ test images for video memory
def image_gradient() -> np.ndarray:
    img = np.zeros((R.HEIGHT, R.WIDTH, 3), np.uint8)                       # B, G, R order
    img[..., 0] = np.linspace(0, 255, R.WIDTH, dtype=np.uint8)
    img[..., 2] = np.linspace(0, 255, R.HEIGHT, dtype=np.uint8)[:, None]
    return img


def image_bars() -> np.ndarray:
    cols_rgb = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (0, 255, 0),
                (255, 0, 255), (255, 0, 0), (0, 0, 255), (0, 0, 0)]
    img = np.zeros((R.HEIGHT, R.WIDTH, 3), np.uint8)
    w = R.WIDTH // 8
    for i, (r, g, b) in enumerate(cols_rgb):
        img[:, i * w:(i + 1) * w] = (b, g, r)
    return img


def image_checker(size: int = 80) -> np.ndarray:
    yy, xx = np.indices((R.HEIGHT, R.WIDTH))
    on = ((xx // size + yy // size) % 2).astype(np.uint8) * 255
    return np.dstack([on, on, on])


def hex_to_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def png_bytes(bgr: np.ndarray, width: int = 480) -> bytes:
    from PIL import Image
    im = Image.fromarray(bgr[..., ::-1]).resize((width, width * R.HEIGHT // R.WIDTH))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ status text
def status_html(v) -> str:
    """One compact HTML block describing what the hardware is doing."""
    def row(k, val, bad=False):
        colour = "#c00" if bad else "#222"
        return f"<tr><td style='padding-right:1em;color:#666'>{k}</td><td style='color:{colour}'>{val}</td></tr>"

    st = v.status()
    inp = st["input"]
    size = "x".join(str(n) for n in inp["hdmi_size"])
    errs = st["vdma_live_s2mm"]["errors"] + st["vdma_live_mm2s"]["errors"] + st["vdma_vmem_mm2s"]["errors"]
    try:
        drift = f"{v.rtc.drift_against():+.2f} s vs this board"
    except Exception:                                           # clock not yet readable: not fatal
        drift = "n/a"
    rows = [
        row("HDMI input", ("good" if inp["hdmi_good"] else ("locked, not 1280x720" if inp["hdmi_locked"] else "no signal"))
            + f" &nbsp; (last frame {size}, {inp['hdmi_frames']} frames)", not inp["hdmi_good"]),
        row("Source in use", inp["active_source"] + (" (fallback: no valid HDMI)" if inp["fallback_active"] else "")),
        row("Display", "video memory" if st["display"] == "vmem" else "live video"),
        row("Filter", st["filter"]),
        row("Hardware clock", f"{st['clock']} &nbsp; ({drift})"),
        row("DMA errors", ", ".join(errs) if errs else "none", bool(errs)),
        row("Frames out", str(inp["out_frames"])),
    ]
    if inp["frame_abort"]:
        rows.append(row("Note", "a malformed HDMI frame was padded (sticky)", True))
    return "<table>" + "".join(rows) + "</table>"


# ------------------------------------------------------------------ the panel
def build_panel(v, refresh_s: float = 1.0, snapshot_dir: str = SNAPSHOT_DIR):
    """Create the control panel for a started Vidcap and return the top-level widget.

    The returned widget has a `.stop()` method that ends the status refresh thread.
    """
    import ipywidgets as W

    log = W.Output(layout=W.Layout(max_height="8em", overflow="auto"))
    state: Dict[str, object] = {"snapshot": None}
    messages: list = []                                     # also kept here, for scripts and tests

    def guarded(fn: Callable) -> Callable:
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except (VidcapError, ValueError, OSError) as e:
                text = f"{datetime.now():%H:%M:%S}  {e}"
                messages.append(text)
                with log:
                    print(text)
        return wrapper

    # ---- source
    source = W.ToggleButtons(options=[("HDMI input", "hdmi"), ("Test pattern", "test")], value=v.input.source(),
                             description="Source", style={"button_width": "9em"})
    fallback = W.Checkbox(value=True, description="Fall back to the test pattern if HDMI is lost for 5 s",
                          indent=False, layout=W.Layout(width="30em"))
    source.observe(guarded(lambda c: v.use_test_pattern(c["new"] == "test")), names="value")
    fallback.observe(guarded(lambda c: v.input.set_fallback(c["new"])), names="value")

    # ---- display: live or an image from video memory
    images = {"Gradient": image_gradient, "Colour bars": image_bars, "Checkerboard": image_checker,
              "Last snapshot": lambda: state["snapshot"]}
    display_mode = W.ToggleButtons(options=[("Live video", "live"), ("Video memory", "vmem")], value="live",
                                   description="Display", style={"button_width": "9em"})
    image_pick = W.Dropdown(options=list(images), value="Gradient", description="Image")

    def apply_display(_=None):
        if display_mode.value == "live":
            v.show_live()
            return
        try:
            img = images[image_pick.value]()
            if img is None:
                raise VidcapError("no snapshot yet: capture one first")
            v.show_image(img)
        except Exception:
            display_mode.value = "live"          # keep the toggle truthful: the display did not change
            raise

    display_mode.observe(guarded(apply_display), names="value")
    image_pick.observe(guarded(lambda c: apply_display() if display_mode.value == "vmem" else None), names="value")

    # ---- filter
    mode = W.Dropdown(options=list(R.FILTER_MODES), value=v.filter.mode(), description="Filter")
    threshold = W.IntSlider(value=v.filter.threshold() or 128, min=0, max=255, description="Threshold",
                            continuous_update=False)

    def apply_filter(_=None):
        v.set_filter(mode.value, threshold.value)

    mode.observe(guarded(apply_filter), names="value")
    threshold.observe(guarded(apply_filter), names="value")

    # ---- timestamp
    ts_on = W.Checkbox(value=v.overlay.enabled(), description="Show timestamp", indent=False)
    ts_scale = W.IntSlider(value=2, min=1, max=4, description="Size", continuous_update=False)
    ts_margin = W.IntSlider(value=16, min=0, max=100, description="Margin", continuous_update=False)
    ts_fg = W.ColorPicker(value="#ffffff", description="Text", concise=True)
    ts_bg = W.ColorPicker(value="#000000", description="Box", concise=True)
    ts_clear = W.Checkbox(value=False, description="Transparent", indent=False)

    def apply_ts(_=None):
        v.overlay.configure(enable=ts_on.value, scale=ts_scale.value, margin=ts_margin.value,
                            fg=hex_to_rgb(ts_fg.value), bg=None if ts_clear.value else hex_to_rgb(ts_bg.value))

    for w in (ts_on, ts_scale, ts_margin, ts_fg, ts_bg, ts_clear):
        w.observe(guarded(apply_ts), names="value")

    sync = W.Button(description="Set clock from this board", icon="clock-o", layout=W.Layout(width="14em"))
    sync.on_click(guarded(lambda _: v.sync_time()))

    # ---- snapshot
    snap_btn = W.Button(description="Capture snapshot", icon="camera", button_style="primary", layout=W.Layout(width="14em"))
    save_btn = W.Button(description="Save as PNG", icon="save", layout=W.Layout(width="10em"), disabled=True)
    snap_img = W.Image(format="png", width=480)
    snap_info = W.Label("no snapshot yet")

    def do_snapshot(_):
        img = v.snapshot()
        state["snapshot"] = img
        snap_img.value = png_bytes(img)
        snap_info.value = f"captured {datetime.now():%H:%M:%S} (before filtering, from the selected input)"
        save_btn.disabled = False

    def do_save(_):
        from PIL import Image
        os.makedirs(snapshot_dir, exist_ok=True)
        path = os.path.join(snapshot_dir, f"snap_{datetime.now():%Y%m%d_%H%M%S}.png")
        Image.fromarray(state["snapshot"][..., ::-1]).save(path)
        snap_info.value = f"saved {path}"

    snap_btn.on_click(guarded(do_snapshot))
    save_btn.on_click(guarded(do_save))

    # ---- status panel with a refresh thread
    status = W.HTML()
    auto = W.ToggleButton(value=True, description="Auto refresh", icon="refresh", layout=W.Layout(width="11em"))
    refresh_btn = W.Button(description="Refresh now", icon="refresh")
    stop_evt = threading.Event()

    def refresh(_=None):
        try:
            status.value = status_html(v)
        except Exception as e:                                  # a status read must never kill the panel
            status.value = f"<span style='color:#c00'>status unavailable: {e}</span>"

    def loop():
        while not stop_evt.wait(refresh_s):
            if auto.value:
                refresh()

    refresh()
    threading.Thread(target=loop, daemon=True, name="vidcap-status").start()

    panel = W.VBox([
        W.HTML("<h3>vidcap controls</h3>"),
        W.VBox([source, fallback]),
        W.VBox([display_mode, image_pick]),
        W.HTML("<b>Filter</b>"), W.HBox([mode, threshold]),
        W.HTML("<b>Timestamp</b>"), W.HBox([ts_on, ts_clear, ts_fg, ts_bg]), W.HBox([ts_scale, ts_margin]), sync,
        W.HTML("<b>Snapshot</b>"), W.HBox([snap_btn, save_btn]), snap_info, snap_img,
        W.HTML("<b>Status</b>"), W.HBox([auto, refresh_btn]), status,
        W.HTML("<b>Messages</b>"), log,
    ])
    refresh_btn.on_click(refresh)
    panel.stop = stop_evt.set
    panel.widgets = {"source": source, "fallback": fallback, "display": display_mode, "image": image_pick,
                     "mode": mode, "threshold": threshold, "ts_on": ts_on, "ts_scale": ts_scale,
                     "ts_margin": ts_margin, "ts_fg": ts_fg, "ts_bg": ts_bg, "ts_clear": ts_clear,
                     "snap": snap_btn, "save": save_btn, "snap_img": snap_img, "sync": sync, "status": status, "log": log,
                     "snap_info": snap_info}
    panel.refresh = refresh
    panel.messages = messages
    return panel
