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
    VC_MODE_COUNT       = 7
};

// Processes exactly one frame per call; with ap_ctrl_none the hardware restarts it forever.
// `mode` and `threshold` are AXI-Lite registers, sampled once at the start of each frame so
// a change always takes effect on a frame boundary (REQ-FLT-3).
void vc_filter(vc_stream &in, vc_stream &out, ap_uint<3> mode, ap_uint<8> threshold);

#endif
