// vc_video.h - Shared video types for the vidcap HLS cores.
//
// Pixel format on every stream in the pipeline: tdata[23:16] = R, [15:8] = G, [7:0] = B
// (so the little-endian bytes in memory are B, G, R - the order PYNQ and OpenCV expect).
// tuser = start of frame (first pixel), tlast = end of line.
#ifndef VC_VIDEO_H
#define VC_VIDEO_H

#include <ap_int.h>
#include <hls_stream.h>
#include <ap_axi_sdata.h>

// Frame size. 1280x720 for the real build; the C-sim/co-sim configs override it with a
// small frame so tests run in seconds.
#ifndef VC_WIDTH
#define VC_WIDTH 1280
#endif
#ifndef VC_HEIGHT
#define VC_HEIGHT 720
#endif

typedef ap_uint<24> vc_pixel;
typedef hls::axis<vc_pixel, 1, 0, 0, AXIS_ENABLE_DATA | AXIS_ENABLE_LAST | AXIS_ENABLE_USER> vc_beat;
typedef hls::stream<vc_beat> vc_stream;

static inline ap_uint<8> vc_r(vc_pixel p) { return p.range(23, 16); }
static inline ap_uint<8> vc_g(vc_pixel p) { return p.range(15, 8); }
static inline ap_uint<8> vc_b(vc_pixel p) { return p.range(7, 0); }
static inline vc_pixel vc_rgb(ap_uint<8> r, ap_uint<8> g, ap_uint<8> b) {
    return (vc_pixel(r) << 16) | (vc_pixel(g) << 8) | vc_pixel(b);
}
// Luma, weights 77/150/29 over 256 (about 0.30 / 0.59 / 0.11).
static inline ap_uint<8> vc_gray(vc_pixel p) {
    ap_uint<16> s = 77 * vc_r(p) + 150 * vc_g(p) + 29 * vc_b(p);
    return s >> 8;
}

#endif
