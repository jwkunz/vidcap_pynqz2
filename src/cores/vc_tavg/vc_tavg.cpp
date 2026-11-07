// vc_tavg.cpp - Temporal moving average with a running sum. See vc_tavg.h for the method and the memory layout.
//
// One frame per call. Inside a frame the work is done line by line, with the five steps of a line running
// concurrently (HLS DATAFLOW), so the memory transfers of one line overlap the video of its neighbours:
//     rx      take a line from the stream, keep the colour, compute and pack the luma
//     fetch   burst-read the oldest luma line and the previous sum line from DDR
//     compute new sum = old sum + luma - oldest luma, and the average
//     emit    put the line on the output stream
//     store   burst-write the new luma line and the new sum line to DDR
#include "vc_tavg.h"

typedef ap_uint<8> u8;
typedef ap_uint<16> u16;
typedef ap_uint<64> u64;

static inline u8 clamp255(ap_int<16> v) {
    if (v < 0) return 0;
    if (v > 255) return 255;
    return (u8)v;
}

// ---------------------------------------------------------------------------------------------- the 5 steps
static void rx_line(vc_stream &in, vc_beat first, bool use_first, vc_pixel rgb[VC_WIDTH], u8 yc[VC_WIDTH],
                    u8 ye[VC_WIDTH], u64 ys_w[VC_TAVG_LINE_Y]) {
    u64 acc = 0;
    for (int x = 0; x < VC_WIDTH; x++) {
#pragma HLS PIPELINE II = 1
        vc_beat b = (use_first && x == 0) ? first : in.read();
        const u8 yy = vc_gray(b.data);
        rgb[x] = b.data;
        yc[x] = yy;
        ye[x] = yy;
        acc = (acc >> 8) | ((u64)yy << 56);               // pack 8 bytes per word, first pixel in the low byte
        if ((x & 7) == 7) ys_w[x >> 3] = acc;
    }
}

static void fetch_line(const u64 *mem, int line, bool have_prev, bool sub, int old_base, int sum_base,
                       u64 old_w[VC_TAVG_LINE_Y], u64 sprev_w[VC_TAVG_LINE_S]) {
    if (sub) {
        const int a = old_base + line * VC_TAVG_LINE_Y;
    read_old:
        for (int i = 0; i < VC_TAVG_LINE_Y; i++) {
#pragma HLS PIPELINE II = 1
            old_w[i] = mem[a + i];
        }
    }
    if (have_prev) {
        const int a = sum_base + line * VC_TAVG_LINE_S;
    read_sum:
        for (int i = 0; i < VC_TAVG_LINE_S; i++) {
#pragma HLS PIPELINE II = 1
            sprev_w[i] = mem[a + i];
        }
    }
}

static void compute_line(const u8 yc[VC_WIDTH], const u64 old_w[VC_TAVG_LINE_Y], const u64 sprev_w[VC_TAVG_LINE_S],
                         bool have_prev, bool sub, ap_uint<17> recip, u8 avg[VC_WIDTH], u64 snew_w[VC_TAVG_LINE_S]) {
    u64 acc = 0;
    for (int i = 0; i < VC_WIDTH; i++) {
#pragma HLS PIPELINE II = 1
        const u8 y = yc[i];
        const u64 ow = old_w[i >> 3];
        const u64 sw = sprev_w[i >> 2];
        const u8 o = sub ? (u8)(ow >> (8 * (i & 7))) : (u8)0;
        const u16 sp = have_prev ? (u16)(sw >> (16 * (i & 3))) : (u16)0;
        const ap_uint<17> t = (ap_uint<17>)sp + y;
        const u16 s = (u16)(t - o);                        // the oldest frame is part of sp, so this cannot go negative
        const ap_uint<34> prod = (ap_uint<34>)s * recip + 0x8000;
        const ap_uint<18> q = prod >> 16;
        avg[i] = (q > 255) ? (u8)255 : (u8)q;
        acc = (acc >> 16) | ((u64)s << 48);
        if ((i & 3) == 3) snew_w[i >> 2] = acc;
    }
}

static void emit_line(vc_stream &out, int line, const vc_pixel rgb[VC_WIDTH], const u8 ye[VC_WIDTH],
                      const u8 avg[VC_WIDTH]) {
    for (int x = 0; x < VC_WIDTH; x++) {
#pragma HLS PIPELINE II = 1
        const ap_int<10> d = (ap_int<10>)avg[x] - (ap_int<10>)ye[x];   // brightness change; colour is kept
        const vc_pixel p = rgb[x];
        vc_beat o;
        o.data = vc_rgb(clamp255((ap_int<16>)vc_r(p) + d), clamp255((ap_int<16>)vc_g(p) + d),
                        clamp255((ap_int<16>)vc_b(p) + d));
        o.user = (line == 0 && x == 0) ? 1 : 0;
        o.last = (x == VC_WIDTH - 1) ? 1 : 0;
        out.write(o);
    }
}

static void store_line(u64 *mem, int line, int wr_base, int sum_base, const u64 ys_w[VC_TAVG_LINE_Y],
                       const u64 snew_w[VC_TAVG_LINE_S]) {
    const int a = wr_base + line * VC_TAVG_LINE_Y;
write_y:
    for (int i = 0; i < VC_TAVG_LINE_Y; i++) {
#pragma HLS PIPELINE II = 1
        mem[a + i] = ys_w[i];
    }
    const int b = sum_base + line * VC_TAVG_LINE_S;
write_s:
    for (int i = 0; i < VC_TAVG_LINE_S; i++) {
#pragma HLS PIPELINE II = 1
        mem[b + i] = snew_w[i];
    }
}

// Disabled: the frame goes straight through. The first beat was already read to find the start of the frame, so it
// is handed in (the same shape as rx_line, which the hardware handles correctly).
static void pass_frame(vc_stream &in, vc_stream &out, vc_beat first) {
    // the first beat goes out on its own, so the loop below has no conditional stream read
    vc_beat o0;
    o0.data = first.data;
    o0.user = 1;
    o0.last = 0;
    out.write(o0);
    for (int t = 1; t < VC_WIDTH * VC_HEIGHT; t++) {
#pragma HLS PIPELINE II = 1
        vc_beat b = in.read();
        vc_beat o;
        o.data = b.data;
        o.user = 0;
        o.last = ((t % VC_WIDTH) == VC_WIDTH - 1) ? 1 : 0;
        out.write(o);
    }
}

static vc_beat wait_for_frame_start(vc_stream &in) {
#pragma HLS INLINE off
    vc_beat b;
    do {
        b = in.read();
    } while (!b.user);
    return b;
}

// ---------------------------------------------------------------------------------------------- top level
void vc_tavg(vc_stream &in, vc_stream &out, u64 *mem_r, u64 *mem_w, ap_uint<1> enable, ap_uint<8> n,
             ap_uint<8> slots, ap_uint<8> restart, ap_uint<16> &status) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE m_axi port = mem_r offset = slave bundle = gmem_r depth = VC_TAVG_DEPTH \
    max_read_burst_length = 64 num_read_outstanding = 4 max_write_burst_length = 2 num_write_outstanding = 1
#pragma HLS INTERFACE m_axi port = mem_w offset = slave bundle = gmem_w depth = VC_TAVG_DEPTH \
    max_write_burst_length = 64 num_write_outstanding = 4 max_read_burst_length = 2 num_read_outstanding = 1
#pragma HLS INTERFACE s_axilite port = mem_r bundle = control
#pragma HLS INTERFACE s_axilite port = mem_w bundle = control
#pragma HLS INTERFACE s_axilite port = enable bundle = control
#pragma HLS INTERFACE s_axilite port = n bundle = control
#pragma HLS INTERFACE s_axilite port = slots bundle = control
#pragma HLS INTERFACE s_axilite port = restart bundle = control
#pragma HLS INTERFACE s_axilite port = status bundle = control
// Production builds free-run; co-simulation builds use the default block-level handshake (see vc_filter.cpp).
#ifdef VC_CTRL_NONE
#pragma HLS INTERFACE ap_ctrl_none port = return
#endif

    static ap_uint<8> cnt = 0;            // frames in the window
    static ap_uint<8> frames = 0;         // frames processed since the last restart
    static ap_uint<8> slot = 0;           // ring slot the next frame will be written to
    static ap_uint<8> last_n = 0, last_slots = 0, last_restart = 0;
    static bool was_enabled = false;

    // Resynchronise to the start of a frame. This is a separate, non-inlined function so that its result reaches the
    // rest of the design through a registered handshake: with the search inlined, the pass-through path below used
    // the beat one cycle before it had been captured (RTL co-simulation showed the previous frame's first pixel).
    const vc_beat first = wait_for_frame_start(in);

    // Latch the controls for the whole frame.
    const bool en = enable;
    const int slots_l = (slots == 0) ? 1 : (int)slots;
    int n_l = (n == 0) ? 1 : (int)n;
    if (n_l > slots_l) n_l = slots_l;

    if (!en) {
        // Disabled: plain pass-through, no memory traffic, and the history is forgotten.
        was_enabled = false;
        cnt = 0;
        frames = 0;
        status = 0;
        pass_frame(in, out, first);
        return;
    }

    const bool restart_now = !was_enabled || n_l != (int)last_n || slots_l != (int)last_slots || restart != last_restart;
    if (restart_now) {
        cnt = 0;
        frames = 0;
        slot = 0;
    }
    last_n = n_l;
    last_slots = slots_l;
    last_restart = restart;
    was_enabled = true;

    const bool sub = ((int)cnt == n_l);                    // the window is full: the oldest frame leaves
    const bool have_prev = (cnt != 0);
    const int new_cnt = sub ? (int)cnt : (int)cnt + 1;
    const ap_uint<17> recip = (ap_uint<17>)((65536 + new_cnt / 2) / new_cnt);

    int old_slot = (int)slot + slots_l - n_l;
    if (old_slot >= slots_l) old_slot -= slots_l;
    const int wr_base = (int)slot * VC_TAVG_Y_WORDS;
    const int old_base = old_slot * VC_TAVG_Y_WORDS;
    const int sum_base = slots_l * VC_TAVG_Y_WORDS;

line_loop:
    for (int line = 0; line < VC_HEIGHT; line++) {
#pragma HLS DATAFLOW
        vc_pixel rgb[VC_WIDTH];
        u8 yc[VC_WIDTH], ye[VC_WIDTH], avg[VC_WIDTH];
        u64 ys_w[VC_TAVG_LINE_Y], old_w[VC_TAVG_LINE_Y], sprev_w[VC_TAVG_LINE_S], snew_w[VC_TAVG_LINE_S];
        rx_line(in, first, line == 0, rgb, yc, ye, ys_w);
        fetch_line(mem_r, line, have_prev, sub, old_base, sum_base, old_w, sprev_w);
        compute_line(yc, old_w, sprev_w, have_prev, sub, recip, avg, snew_w);
        emit_line(out, line, rgb, ye, avg);
        store_line(mem_w, line, wr_base, sum_base, ys_w, snew_w);
    }

    cnt = new_cnt;
    frames = frames + 1;
    slot = (slot + 1 >= slots_l) ? (ap_uint<8>)0 : (ap_uint<8>)(slot + 1);
    status = ((ap_uint<16>)frames << 8) | (ap_uint<16>)cnt;
}
