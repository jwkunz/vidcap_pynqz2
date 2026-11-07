// vc_snapshot.cpp - Pass-through video core that can also store one frame in DDR.
//
// Per line: pixels are packed 8 at a time (24 bytes = three 64-bit words) into a line buffer
// while they are forwarded; at the end of the line the buffer is burst-written to DDR. Only
// the capture frame pays for the burst (about 480 clocks per line); idle frames just pass.
#include "vc_snapshot.h"

void vc_snapshot(vc_stream &in, vc_stream &out, ap_uint<64> *mem,
                 ap_uint<8> trigger, ap_uint<8> ack,
                 ap_uint<16> &status, ap_uint<1> &irq) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE m_axi port = mem offset = slave bundle = gmem depth = VC_SNAP_DEPTH \
    max_write_burst_length = 256 num_write_outstanding = 4 max_read_burst_length = 2 num_read_outstanding = 1
#pragma HLS INTERFACE s_axilite port = mem bundle = control
#pragma HLS INTERFACE s_axilite port = trigger bundle = control
#pragma HLS INTERFACE s_axilite port = ack bundle = control
#pragma HLS INTERFACE s_axilite port = status bundle = control
#pragma HLS INTERFACE ap_none port = irq
// Production builds free-run (no start/done handshake). Vitis HLS co-simulation cannot drive
// ap_ctrl_none designs of this shape, so co-sim builds leave it out and use the default
// block-level handshake; the pixel datapath is identical.
#ifdef VC_CTRL_NONE
#pragma HLS INTERFACE ap_ctrl_none port = return
#endif

    static ap_uint<8> served = 0;     // trigger value of the last capture started
    static ap_uint<8> done_cnt = 0;   // captures completed
    static ap_uint<64> lb[VC_SNAP_WORDS_PER_LINE];
#pragma HLS BIND_STORAGE variable = lb type = ram_2p impl = bram

    // Wait for the start of a frame (the upstream always delivers whole frames).
    vc_beat first;
    do {
        first = in.read();
    } while (!first.user);

    const ap_uint<8> trig_l = trigger;
    const bool cap = (trig_l != served);
    status = ((ap_uint<16>)done_cnt << 8) | (cap ? 1 : 0);
    irq = (done_cnt != ack) ? 1 : 0;

row_loop:
    for (int y = 0; y < VC_HEIGHT; y++) {
        ap_uint<192> acc = 0;
        ap_uint<64> w1 = 0, w2 = 0;
        int ph = 0;          // pixel phase within a group of 8
        int grp = 0;         // group of 8 pixels within the line
        int wbase = 0;       // line-buffer index of the group being written out
        bool s1 = false, s2 = false;

pixel_loop:
        for (int xx = 0; xx < VC_WIDTH + 2; xx++) {   // two extra clocks flush the last group's words
#pragma HLS PIPELINE II = 1
#pragma HLS DEPENDENCE variable = lb type = inter dependent = false
            if (xx < VC_WIDTH) {
                vc_beat b = (y == 0 && xx == 0) ? first : in.read();
                out.write(b);
                irq = (done_cnt != ack) ? 1 : 0;
                if (cap) {
                    acc = (acc >> 24) | ((ap_uint<192>)b.data << 168);
                    if (ph == 7) {
                        wbase = 3 * grp;
                        lb[wbase] = acc.range(63, 0);
                        w1 = acc.range(127, 64);
                        w2 = acc.range(191, 128);
                        s1 = true;
                        grp++;
                        ph = 0;
                    } else {
                        ph++;
                        if (s1) { lb[wbase + 1] = w1; s1 = false; s2 = true; }
                        else if (s2) { lb[wbase + 2] = w2; s2 = false; }
                    }
                }
            } else if (cap) {
                // flush clocks after the last pixel
                if (s1) { lb[wbase + 1] = w1; s1 = false; s2 = true; }
                else if (s2) { lb[wbase + 2] = w2; s2 = false; }
            }
        }

        if (cap) {
write_loop:
            for (int i = 0; i < VC_SNAP_WORDS_PER_LINE; i++) {
#pragma HLS PIPELINE II = 1
                mem[y * VC_SNAP_WORDS_PER_LINE + i] = lb[i];
            }
        }
    }

    if (cap) {
        served = trig_l;
        done_cnt = done_cnt + 1;
    }
    status = ((ap_uint<16>)done_cnt << 8);
    irq = (done_cnt != ack) ? 1 : 0;
}
