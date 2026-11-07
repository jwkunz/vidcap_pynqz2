// vc_overlay.cpp - Draws the hardware clock's date and time over the video.
//
// One frame per call (the hardware restarts the function). Everything configurable is sampled
// once at the start of the frame, so the text never changes mid-frame.
#include "vc_overlay.h"
#include "vc_font.h"

void vc_overlay(vc_stream &in, vc_stream &out,
                ap_uint<56> time_digits,
                ap_uint<1> enable, ap_uint<3> scale, ap_uint<8> margin,
                ap_uint<24> fg, ap_uint<24> bg, ap_uint<1> bg_en) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE ap_none port = time_digits
#pragma HLS INTERFACE s_axilite port = enable bundle = control
#pragma HLS INTERFACE s_axilite port = scale bundle = control
#pragma HLS INTERFACE s_axilite port = margin bundle = control
#pragma HLS INTERFACE s_axilite port = fg bundle = control
#pragma HLS INTERFACE s_axilite port = bg bundle = control
#pragma HLS INTERFACE s_axilite port = bg_en bundle = control
// Production builds free-run (no start/done handshake). Vitis HLS co-simulation cannot drive
// ap_ctrl_none designs of this shape, so co-sim builds leave it out and use the default
// block-level handshake; the pixel datapath is identical.
#ifdef VC_CTRL_NONE
#pragma HLS INTERFACE ap_ctrl_none port = return
#endif

    // Resynchronise to a start-of-frame beat.
    vc_beat first;
    do {
        first = in.read();
    } while (!first.user);

    // ---- latch everything for this frame
    const ap_uint<56> td = time_digits;
    const bool en = enable;
    const int s = (scale == 0) ? 1 : (scale > 4 ? 4 : (int)scale);
    const vc_pixel fg_l = fg;
    const vc_pixel bg_l = bg;
    const bool bg_on = bg_en;

    // glyph index per character: 0-9 digits, 10 '-', 11 ':', 12 ' '
    ap_uint<4> chars[VC_OVERLAY_CHARS];
#pragma HLS ARRAY_PARTITION variable = chars complete
    ap_uint<4> nib[14];
    for (int k = 0; k < 14; k++) {
#pragma HLS UNROLL
        ap_uint<4> d = td.range(55 - 4 * k, 52 - 4 * k);
        nib[k] = (d > 9) ? ap_uint<4>(12) : d;
    }
    chars[0] = nib[0];  chars[1] = nib[1];  chars[2] = nib[2];  chars[3] = nib[3];
    chars[4] = 10;
    chars[5] = nib[4];  chars[6] = nib[5];
    chars[7] = 10;
    chars[8] = nib[6];  chars[9] = nib[7];
    chars[10] = 12;
    chars[11] = nib[8]; chars[12] = nib[9];
    chars[13] = 11;
    chars[14] = nib[10]; chars[15] = nib[11];
    chars[16] = 11;
    chars[17] = nib[12]; chars[18] = nib[13];

    const int box_w = VC_OVERLAY_CHARS * VC_GLYPH_W * s;
    const int box_h = VC_GLYPH_H * s;
    const int tx0 = VC_WIDTH - (int)margin - box_w;
    const int ty0 = VC_HEIGHT - (int)margin - box_h;
    const int tx1 = tx0 + box_w;
    const int ty1 = ty0 + box_h;
    const bool fits = (tx0 >= 0) && (ty0 >= 0);

    int xx = 0, yy = 0;
    int cidx = 0, sx = 0, px = 0;   // horizontal position inside the text box
    int ry = 0, py = 0;             // vertical position inside the text box

pixel_loop:
    for (int t = 0; t < VC_WIDTH * VC_HEIGHT; t++) {
#pragma HLS PIPELINE II = 1
        vc_beat b = (t == 0) ? first : in.read();
        vc_pixel pix = b.data;

        const bool in_x = (xx >= tx0) && (xx < tx1);
        const bool in_y = (yy >= ty0) && (yy < ty1);

        // horizontal counters for this pixel (reset on the first text column)
        int c_cidx = (xx == tx0) ? 0 : cidx;
        int c_sx = (xx == tx0) ? 0 : sx;
        int c_px = (xx == tx0) ? 0 : px;

        if (en && fits && in_x && in_y) {
            ap_uint<4> g = chars[c_cidx];
            ap_uint<8> row = vc_font[g][py];
            bool on = (row >> (7 - c_px)) & 1;
            if (on) pix = fg_l;
            else if (bg_on) pix = bg_l;
        }

        // advance the horizontal counters
        int n_sx = c_sx + 1, n_px = c_px, n_cidx = c_cidx;
        if (n_sx == s) {
            n_sx = 0;
            n_px = c_px + 1;
            if (n_px == VC_GLYPH_W) { n_px = 0; n_cidx = c_cidx + 1; }
        }
        cidx = n_cidx; sx = n_sx; px = n_px;

        vc_beat o;
        o.data = pix;
        o.user = (xx == 0 && yy == 0) ? 1 : 0;
        o.last = (xx == VC_WIDTH - 1) ? 1 : 0;
        out.write(o);

        // advance the frame position; at the end of a text line step the vertical counters
        if (xx == VC_WIDTH - 1) {
            xx = 0;
            yy++;
            if (in_y) {
                ry++;
                if (ry == s) { ry = 0; py++; }
            }
        } else {
            xx++;
        }
    }
}
