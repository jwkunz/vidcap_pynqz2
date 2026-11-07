// vc_filter.h - Streaming video filter (REQ-FLT-*).
#ifndef VC_FILTER_H
#define VC_FILTER_H
#include "vc_video.h"

enum vc_filter_mode {
    VC_MODE_PASSTHROUGH = 0,   // unchanged (power-up default)
    VC_MODE_INVERT      = 1,
    VC_MODE_GRAYSCALE   = 2,
    VC_MODE_THRESHOLD   = 3,   // luma > threshold ? white : black
    VC_MODE_SOBEL       = 4,   // edge magnitude, 3x3 Sobel on luma
    VC_MODE_BLUR        = 5,   // 3x3 Gaussian
    VC_MODE_SHARPEN     = 6,   // 3x3 sharpen (centre 5, 4-neighbours -1)
    VC_MODE_NOISE       = 7,   // retro-VCR grain: LFSR noise added to every pixel
    VC_MODE_COUNT       = 8    // the mode register is 3 bits wide, so 7 is the highest code
};

// ---- noise: a free-running 32-bit Galois LFSR (taps 32,22,2,1), advanced 8 steps per output pixel so each
// pixel gets 8 fresh bits. The low byte is a signed sample s (-128..127); all three colour channels of the
// pixel change by the same delta = (s * amount) >> 8, i.e. by at most +-amount/2 (amount 255 = about +-127),
// which is grey "snow", as on a VCR. The state is never reset between frames, so the grain changes every frame.
#define VC_LFSR_SEED 0xACE1ACE1u
static inline ap_uint<32> vc_lfsr_step8(ap_uint<32> s) {
    for (int i = 0; i < 8; i++) {
        ap_uint<1> lsb = s[0];
        s = s >> 1;
        if (lsb) s ^= ap_uint<32>(0x80200003u);
    }
    return s;
}

// Processes exactly one frame per call; with ap_ctrl_none the hardware restarts it forever.
// `mode`, `threshold` and `noise_amount` are AXI-Lite registers, sampled once at the start of each frame so
// a change always takes effect on a frame boundary (REQ-FLT-3). noise_amount: 0 = none .. 255 = +-127.
void vc_filter(vc_stream &in, vc_stream &out, ap_uint<3> mode, ap_uint<8> threshold, ap_uint<8> noise_amount);

#endif
