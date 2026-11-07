// vc_tavg.h - Temporal moving average over the last N frames, with a programmable N (REQ-FLT, temporal).
#ifndef VC_TAVG_H
#define VC_TAVG_H
#include "vc_video.h"

// Method: a running sum, so the DDR traffic is the same for any N.
//     S[t] = S[t-1] + Y[t] - Y[t-N]        average[t] = S[t] / N
// Y is the 8-bit luma of a frame. The output keeps the current frame's colour and replaces its brightness with
// the temporal average: out = clamp(rgb + (average - Y_now)) on every channel. The sum is exact integer
// arithmetic (a 16-bit sum holds up to 255 frames of 255), so nothing drifts.
//
// While fewer than N frames have been seen since the filter was (re)started, it averages the frames it has
// (1, 2, 3 ... N), so the first output is the input unchanged.
//
// Memory (one contiguous buffer, base address written to BOTH mem_r and mem_w):
//     [ ring: `slots` luma frames, W*H bytes each ][ sum frame: W*H 16-bit values ]
// Software chooses `slots` (>= N). Frame t is stored in ring slot (t mod slots); the frame that leaves the
// window is read from slot (t - N) mod slots. DDR traffic per frame: read oldest luma, read sum, write sum,
// write new luma = 1+2+2+1 bytes per pixel, about 330 MB/s at 720p60.
//
// Control (AXI-Lite, sampled once at the start of each frame):
//   enable   0 = pass the video through untouched and do no memory traffic (history is discarded)
//   n        window length 1..255 (clamped to `slots`)
//   slots    ring size, 1..255, set to the number of frames the buffer holds
//   restart  8-bit counter: writing a different value restarts the average (empties the window)
//   The average also restarts by itself when enable goes 0 -> 1 or when n or slots change.
// status (read-only): [7:0] frames currently in the average, [15:8] frames processed since the last restart.
#define VC_TAVG_Y_WORDS (VC_WIDTH * VC_HEIGHT / 8)            // one luma frame, in 64-bit words
#define VC_TAVG_S_WORDS (VC_WIDTH * VC_HEIGHT * 2 / 8)        // the 16-bit sum frame
#define VC_TAVG_LINE_Y  (VC_WIDTH / 8)                        // words per luma line (W must be a multiple of 8)
#define VC_TAVG_LINE_S  (VC_WIDTH * 2 / 8)

// m_axi port depth for co-simulation: the run script passes the size of the testbench's buffer.
#ifndef VC_TAVG_DEPTH
#define VC_TAVG_DEPTH (255 * VC_TAVG_Y_WORDS + VC_TAVG_S_WORDS)
#endif

void vc_tavg(vc_stream &in, vc_stream &out, ap_uint<64> *mem_r, ap_uint<64> *mem_w,
             ap_uint<1> enable, ap_uint<8> n, ap_uint<8> slots, ap_uint<8> restart,
             ap_uint<16> &status);

#endif
