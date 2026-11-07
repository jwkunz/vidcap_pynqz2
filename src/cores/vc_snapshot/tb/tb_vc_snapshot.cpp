// C-sim / co-sim testbench for vc_snapshot.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include "../vc_snapshot.h"

typedef std::vector<uint32_t> Frame;   // 0x00RRGGBB

static Frame make_frame(uint32_t seed) {
    Frame f(VC_WIDTH * VC_HEIGHT);
    for (auto &p : f) { seed = seed * 1664525u + 1013904223u; p = (seed >> 8) & 0xFFFFFF; }
    return f;
}

static const int MEM_WORDS = VC_SNAP_WORDS_PER_LINE * VC_HEIGHT;

// returns the number of stream/memory errors
static int run(const char *name, const Frame &f, int trigger, int ack, bool expect_capture,
               std::vector<ap_uint<64>> &mem, int expect_done, int expect_irq) {
    vc_stream in, out;
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            vc_beat b; b.data = f[y * VC_WIDTH + x]; b.user = (x == 0 && y == 0); b.last = (x == VC_WIDTH - 1);
            in.write(b);
        }
    std::vector<ap_uint<64>> before = mem;
    ap_uint<16> status; ap_uint<1> irq;
    vc_snapshot(in, out, mem.data(), trigger, ack, status, irq);

    int errs = 0;
    // 1. the video must come through unchanged, with correct framing
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            if (out.empty()) { printf("FAIL %s: output ended early\n", name); return 1; }
            vc_beat b = out.read();
            if ((uint32_t)b.data != f[y * VC_WIDTH + x] || (int)b.user != (x == 0 && y == 0) || (int)b.last != (x == VC_WIDTH - 1)) errs++;
        }
    if (!out.empty()) errs++;
    int stream_errs = errs;

    // 2. memory: captured exactly when expected, bytes in B,G,R order
    int mem_errs = 0;
    if (expect_capture) {
        const uint8_t *bytes = reinterpret_cast<const uint8_t *>(mem.data());
        for (int i = 0; i < VC_WIDTH * VC_HEIGHT; i++) {
            uint32_t p = f[i];
            uint8_t eb = p & 255, eg = (p >> 8) & 255, er = (p >> 16) & 255;
            if (bytes[3 * i] != eb || bytes[3 * i + 1] != eg || bytes[3 * i + 2] != er) mem_errs++;
        }
    } else {
        for (int i = 0; i < MEM_WORDS; i++) if (mem[i] != before[i]) mem_errs++;
    }
    errs += mem_errs;

    // 3. status / irq
    int done = (int)status.range(15, 8), busy = (int)status.range(0, 0);
    int st_errs = (done != expect_done) + (busy != 0) + ((int)irq != expect_irq);
    errs += st_errs;
    printf("%s %s (stream errors %d, memory errors %d, done=%d irq=%d)\n", errs ? "FAIL" : "ok  ", name,
           stream_errs, mem_errs, done, (int)irq);
    return errs ? 1 : 0;
}

int main() {
    std::vector<ap_uint<64>> mem(MEM_WORDS, ap_uint<64>(0xDEADBEEFDEADBEEFull));
    Frame a = make_frame(1), b = make_frame(2), c = make_frame(3);
    int fails = 0;

    fails += run("idle: video passes, memory untouched", a, /*trigger*/ 0, /*ack*/ 0, false, mem, 0, 0);
    fails += run("trigger 1: captures this frame", b, 1, 0, true, mem, 1, 1);
    fails += run("no new trigger: no capture, irq still up", c, 1, 0, false, mem, 1, 1);
    fails += run("ack clears the interrupt", c, 1, 1, false, mem, 1, 0);
    fails += run("trigger 2: captures the next frame", a, 2, 1, true, mem, 2, 1);
    fails += run("ack 2", b, 2, 2, false, mem, 2, 0);
    fails += run("trigger jumps by more than one (wrap-safe)", c, 200, 2, true, mem, 3, 1);
    if (fails) { printf("TEST FAILED (%d)\n", fails); return 1; }
    printf("TEST PASSED\n");
    return 0;
}
