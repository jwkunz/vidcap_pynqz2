// vc_snapshot.h - Software-triggered single-frame capture to DDR (REQ-SNAP-*).
#ifndef VC_SNAPSHOT_H
#define VC_SNAPSHOT_H
#include "vc_video.h"

// The core sits IN SERIES in the video stream: every pixel is passed straight through, so an
// idle snapshot never stalls the pipeline. When software triggers it, it also writes the next
// complete frame to memory as packed 24-bit pixels in memory byte order B,G,R (3 bytes/pixel,
// W*H*3 bytes, rows back to back).
//
// Control (AXI-Lite):
//   mem      physical byte address of the snapshot buffer (set before triggering)
//   trigger  8-bit counter. Writing a value different from the number of captures started
//            arms a capture of the next frame. Software writes (last trigger + 1).
//   ack      8-bit counter. Software writes the done count it has handled. The irq output
//            stays high while done count != ack.
//   status   [0] busy (a capture is in progress)   [15:8] done count (captures completed)
// Output:
//   irq      level interrupt: done count != ack
void vc_snapshot(vc_stream &in, vc_stream &out, ap_uint<64> *mem,
                 ap_uint<8> trigger, ap_uint<8> ack,
                 ap_uint<16> &status, ap_uint<1> &irq);

#define VC_SNAP_WORDS_PER_LINE (VC_WIDTH * 3 / 8)   // 64-bit words per line (W must be a multiple of 8)
// Port depth for C/RTL co-simulation: the number of 64-bit words in one frame. The run script passes
// it in (-DVC_SNAP_DEPTH) so it always matches the testbench's array; the default is the real frame.
#ifndef VC_SNAP_DEPTH
#define VC_SNAP_DEPTH 345600
#endif

#endif
