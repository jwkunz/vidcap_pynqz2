// vc_filter.cpp - One pipeline for all modes: a 3x3 window over a 1-pixel-per-clock stream.
//
// Every mode (even passthrough) reads the centre of the window, so the frame is delayed by
// one line + one pixel regardless of mode and the output is always the same size. Frame
// edges are handled by replicating the nearest pixel.
#include "vc_filter.h"

static inline ap_uint<8> clamp255(ap_int<16> v) {
    if (v < 0) return 0;
    if (v > 255) return 255;
    return (ap_uint<8>)v;
}

void vc_filter(vc_stream &in, vc_stream &out, ap_uint<3> mode, ap_uint<8> threshold, ap_uint<8> noise_amount) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE s_axilite port = mode bundle = control
#pragma HLS INTERFACE s_axilite port = threshold bundle = control
#pragma HLS INTERFACE s_axilite port = noise_amount bundle = control
// Production builds free-run (no start/done handshake). Vitis HLS co-simulation cannot drive
// ap_ctrl_none designs of this shape, so co-sim builds leave it out and use the default
// block-level handshake; the pixel datapath is identical.
#ifdef VC_CTRL_NONE
#pragma HLS INTERFACE ap_ctrl_none port = return
#endif

    static vc_pixel lb0[VC_WIDTH];   // previous row
    static vc_pixel lb1[VC_WIDTH];   // the row before that
#pragma HLS BIND_STORAGE variable = lb0 type = ram_2p impl = bram
#pragma HLS BIND_STORAGE variable = lb1 type = ram_2p impl = bram

    // Resynchronise: discard everything up to the first start-of-frame beat.
    vc_beat first;
    do {
        first = in.read();
    } while (!first.user);

    // Latch the controls for the whole frame.
    const ap_uint<3> mode_l = mode;
    const ap_uint<8> thr_l = threshold;
    const ap_uint<8> amt_l = noise_amount;

    // Free-running noise generator: its state survives from frame to frame (the hardware never resets it
    // after power-up), so the grain is different in every frame.
    static ap_uint<32> lfsr = VC_LFSR_SEED;

    vc_pixel win[3][3];
#pragma HLS ARRAY_PARTITION variable = win complete dim = 0

    const int ROWS = VC_HEIGHT + 1;   // one extra row and column flush the window
    const int COLS = VC_WIDTH + 1;
    int r = 0, c = 0;

frame_loop:
    for (int t = 0; t < ROWS * COLS; t++) {
#pragma HLS PIPELINE II = 1
#pragma HLS DEPENDENCE variable = lb0 type = inter dependent = false
#pragma HLS DEPENDENCE variable = lb1 type = inter dependent = false

        // ---- fetch the incoming column: rows (r-2, r-1, r) at column c
        vc_pixel colv[3];
#pragma HLS ARRAY_PARTITION variable = colv complete
        if (c < VC_WIDTH) {
            vc_pixel p_in = 0;
            if (r < VC_HEIGHT) {
                vc_beat b = (t == 0) ? first : in.read();
                p_in = b.data;
            }
            vc_pixel top = lb1[c];
            vc_pixel mid = lb0[c];
            colv[0] = top;
            colv[1] = mid;
            colv[2] = (r < VC_HEIGHT) ? p_in : mid;
            lb1[c] = mid;
            lb0[c] = p_in;
        } else {
            colv[0] = win[0][2];
            colv[1] = win[1][2];
            colv[2] = win[2][2];
        }

        // ---- shift the window left and insert the new column
        for (int i = 0; i < 3; i++) {
#pragma HLS UNROLL
            win[i][0] = win[i][1];
            win[i][1] = win[i][2];
            win[i][2] = colv[i];
        }

        // ---- produce the output pixel for the window centre (x = c-1, y = r-1)
        if (r >= 1 && c >= 1) {
            const int x = c - 1;
            const int y = r - 1;
            vc_pixel n[3][3];
#pragma HLS ARRAY_PARTITION variable = n complete dim = 0
            for (int i = 0; i < 3; i++) {
#pragma HLS UNROLL
                // edge replication: above the first row / below the last row / left of the first
                // column / right of the last column use the centre line or column instead
                const int ri = (i == 0 && y == 0) ? 1 : (i == 2 && y == VC_HEIGHT - 1) ? 1 : i;
                for (int j = 0; j < 3; j++) {
#pragma HLS UNROLL
                    const int cj = (j == 0 && x == 0) ? 1 : (j == 2 && x == VC_WIDTH - 1) ? 1 : j;
                    n[i][j] = win[ri][cj];
                }
            }
            const vc_pixel ctr = n[1][1];

            // luma of the whole window
            ap_uint<8> g[3][3];
#pragma HLS ARRAY_PARTITION variable = g complete dim = 0
            for (int i = 0; i < 3; i++) {
#pragma HLS UNROLL
                for (int j = 0; j < 3; j++) {
#pragma HLS UNROLL
                    g[i][j] = vc_gray(n[i][j]);
                }
            }

            // Sobel
            ap_int<14> gx = (ap_int<14>)(g[0][2] + 2 * g[1][2] + g[2][2]) -
                            (ap_int<14>)(g[0][0] + 2 * g[1][0] + g[2][0]);
            ap_int<14> gy = (ap_int<14>)(g[2][0] + 2 * g[2][1] + g[2][2]) -
                            (ap_int<14>)(g[0][0] + 2 * g[0][1] + g[0][2]);
            ap_int<16> agx = gx < 0 ? ap_int<16>(-gx) : ap_int<16>(gx);
            ap_int<16> agy = gy < 0 ? ap_int<16>(-gy) : ap_int<16>(gy);
            ap_int<16> mag = agx + agy;
            ap_uint<8> sob = clamp255(mag);

            // Blur and sharpen, per channel
            ap_uint<8> blur_c[3], sharp_c[3];
            for (int ch = 0; ch < 3; ch++) {
#pragma HLS UNROLL
                ap_uint<8> v[3][3];
                for (int i = 0; i < 3; i++) {
#pragma HLS UNROLL
                    for (int j = 0; j < 3; j++) {
#pragma HLS UNROLL
                        v[i][j] = n[i][j].range(23 - 8 * ch, 16 - 8 * ch);
                    }
                }
                ap_uint<14> bsum = v[0][0] + 2 * v[0][1] + v[0][2] + 2 * v[1][0] + 4 * v[1][1] +
                                   2 * v[1][2] + v[2][0] + 2 * v[2][1] + v[2][2] + 8;
                blur_c[ch] = bsum >> 4;
                ap_int<14> sv = 5 * (ap_int<14>)v[1][1] - (ap_int<14>)v[0][1] - (ap_int<14>)v[1][0] -
                                (ap_int<14>)v[1][2] - (ap_int<14>)v[2][1];
                sharp_c[ch] = clamp255(sv);
            }

            // Noise: one LFSR update per output pixel in EVERY mode, so the sequence depends only on how many
            // pixels have been output, never on which mode was selected.
            lfsr = vc_lfsr_step8(lfsr);
            const ap_int<9> ns = (ap_int<9>)(ap_int<8>)lfsr.range(7, 0);           // signed -128..127
            const ap_int<18> nprod = ns * (ap_int<10>)amt_l;
            const ap_int<10> ndelta = (ap_int<10>)(nprod >> 8);
            const vc_pixel noisy = vc_rgb(clamp255((ap_int<16>)vc_r(ctr) + ndelta),
                                          clamp255((ap_int<16>)vc_g(ctr) + ndelta),
                                          clamp255((ap_int<16>)vc_b(ctr) + ndelta));

            // Select
            vc_pixel result;
            const ap_uint<8> gc = g[1][1];
            switch (mode_l) {
            case VC_MODE_INVERT:    result = ~ctr; break;
            case VC_MODE_GRAYSCALE: result = vc_rgb(gc, gc, gc); break;
            case VC_MODE_THRESHOLD: result = (gc > thr_l) ? vc_pixel(0xFFFFFF) : vc_pixel(0); break;
            case VC_MODE_SOBEL:     result = vc_rgb(sob, sob, sob); break;
            case VC_MODE_BLUR:      result = vc_rgb(blur_c[0], blur_c[1], blur_c[2]); break;
            case VC_MODE_SHARPEN:   result = vc_rgb(sharp_c[0], sharp_c[1], sharp_c[2]); break;
            case VC_MODE_NOISE:     result = noisy; break;
            default:                result = ctr; break;   // passthrough (and any unused code)
            }

            vc_beat o;
            o.data = result;
            o.user = (x == 0 && y == 0) ? 1 : 0;
            o.last = (x == VC_WIDTH - 1) ? 1 : 0;
            out.write(o);
        }

        // ---- advance the row/column counters
        if (c == VC_WIDTH) { c = 0; r++; }
        else c++;
    }
}
