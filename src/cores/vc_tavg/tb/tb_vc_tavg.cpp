// C-sim / co-sim testbench for vc_tavg. The golden model averages a plain list of past frames, so it knows nothing
// about the hardware's ring buffer or running sum; the testbench also checks the ring and the sum frame in memory.
#include <cstdio>
#include <cstdint>
#include <vector>
#include <algorithm>
#include "../vc_tavg.h"

typedef std::vector<uint32_t> Frame;           // 0x00RRGGBB
typedef std::vector<int> Luma;

static const int RING_MAX = 8;
static std::vector<ap_uint<64>> mem(RING_MAX * VC_TAVG_Y_WORDS + VC_TAVG_S_WORDS, ap_uint<64>(0xDEADBEEFDEADBEEFull));

static int R(uint32_t p) { return (p >> 16) & 255; }
static int G(uint32_t p) { return (p >> 8) & 255; }
static int B(uint32_t p) { return p & 255; }
static int clampi(int v) { return v < 0 ? 0 : (v > 255 ? 255 : v); }
static int luma(uint32_t p) { return (77 * R(p) + 150 * G(p) + 29 * B(p)) >> 8; }

struct Golden {
    std::vector<Luma> win;
    bool was_en = false;
    int last_n = 0, last_slots = 0, last_restart = 0;
    int slot = 0, frames = 0;
    int cnt = 0;

    Frame process(const Frame &in, bool en, int n, int slots, int restart) {
        if (!en) { was_en = false; win.clear(); cnt = 0; frames = 0; return in; }
        int s = slots ? slots : 1, nn = n ? n : 1;
        if (nn > s) nn = s;
        if (!was_en || nn != last_n || s != last_slots || restart != last_restart) { win.clear(); slot = 0; frames = 0; }
        was_en = true; last_n = nn; last_slots = s; last_restart = restart;
        Luma y(in.size());
        for (size_t i = 0; i < in.size(); i++) y[i] = luma(in[i]);
        win.push_back(y);
        if ((int)win.size() > nn) win.erase(win.begin());
        cnt = (int)win.size();
        const int recip = (65536 + cnt / 2) / cnt;
        Frame out(in.size());
        for (size_t i = 0; i < in.size(); i++) {
            long sum = 0;
            for (auto &f : win) sum += f[i];
            int avg = (int)std::min<long>(255, (sum * recip + 0x8000) >> 16);
            int d = avg - y[i];
            out[i] = ((uint32_t)clampi(R(in[i]) + d) << 16) | ((uint32_t)clampi(G(in[i]) + d) << 8) | (uint32_t)clampi(B(in[i]) + d);
        }
        return out;
    }
    // where the hardware must have written this frame, and where the sum frame lives
    int cur_slot_written = 0, cur_slots = 1;
    void advance(int slots) { cur_slot_written = slot; cur_slots = slots ? slots : 1; slot = (slot + 1 >= cur_slots) ? 0 : slot + 1; }
};

static Frame make_frame(uint32_t seed) {
    Frame f(VC_WIDTH * VC_HEIGHT);
    for (auto &p : f) { seed = seed * 1664525u + 1013904223u; p = (seed >> 8) & 0xFFFFFF; }
    return f;
}
static Frame make_const(uint32_t rgb) { return Frame(VC_WIDTH * VC_HEIGHT, rgb); }

static uint8_t mem_byte(int word_base, int idx) { return (uint8_t)(mem[word_base + (idx >> 3)] >> (8 * (idx & 7))); }
static int mem_u16(int word_base, int idx) { return (int)(uint16_t)(mem[word_base + (idx >> 2)] >> (16 * (idx & 3))); }

static int fails = 0;
static Golden gold;
static int frame_no = 0;

static void push_frame(vc_stream &s, const Frame &f) {
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            vc_beat b; b.data = f[y * VC_WIDTH + x]; b.user = (x == 0 && y == 0); b.last = (x == VC_WIDTH - 1);
            s.write(b);
        }
}

// one frame through the core, compared with the golden model, memory checked
static void step(const char *what, const Frame &in, bool en, int n, int slots, int restart) {
    std::vector<ap_uint<64>> before = mem;
    vc_stream si, so;
    push_frame(si, in);
    ap_uint<16> status;
    vc_tavg(si, so, mem.data(), mem.data(), en, n, slots, restart, status);
    Frame exp = gold.process(in, en, n, slots, restart);

    int bad = 0, sbad = 0;
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            if (so.empty()) { printf("FAIL %s: output ended early\n", what); fails++; return; }
            vc_beat b = so.read();
            if ((int)b.user != (x == 0 && y == 0) || (int)b.last != (x == VC_WIDTH - 1)) sbad++;
            if ((uint32_t)b.data != exp[y * VC_WIDTH + x]) {
                if (bad < 3) printf("  %s: pixel (%d,%d) got %06x expected %06x\n", what, x, y, (uint32_t)b.data, exp[y * VC_WIDTH + x]);
                bad++;
            }
        }
    if (!so.empty()) { printf("FAIL %s: extra output beats\n", what); fails++; return; }

    int mem_bad = 0;
    if (en) {
        const int slots_l = slots ? slots : 1;
        gold.advance(slots);
        const int ybase = gold.cur_slot_written * VC_TAVG_Y_WORDS;
        for (int i = 0; i < VC_WIDTH * VC_HEIGHT; i++) if (mem_byte(ybase, i) != luma(in[i])) mem_bad++;
        const int sbase = slots_l * VC_TAVG_Y_WORDS;
        for (int i = 0; i < VC_WIDTH * VC_HEIGHT; i++) {
            long sum = 0;
            for (auto &f : gold.win) sum += f[i];
            if (mem_u16(sbase, i) != sum) mem_bad++;
        }
    } else {
        for (size_t i = 0; i < mem.size(); i++) if (mem[i] != before[i]) mem_bad++;      // disabled: no memory traffic
    }
    const int want_cnt = en ? gold.cnt : 0, want_frames = en ? ((gold.frames + 1) & 255) : 0;
    if (en) gold.frames++;
    const int st_bad = (int)status.range(7, 0) != want_cnt || (int)status.range(15, 8) != (en ? (gold.frames & 255) : 0);
    (void)want_frames;
    const bool ok = !bad && !sbad && !mem_bad && !st_bad;
    if (!ok) fails++;
    printf("%s frame %-2d %-34s window %2d  (pixels %d, framing %d, memory %d, status %s)\n", ok ? "ok  " : "FAIL", frame_no, what,
           want_cnt, bad, sbad, mem_bad, st_bad ? "WRONG" : "ok");
    frame_no++;
}

int main() {
    // 1. disabled: untouched video and no memory traffic
    step("disabled", make_frame(1), false, 4, 6, 0);

    // 2. N = 1: the average of one frame is the frame itself
    step("N=1 (identity)", make_frame(2), true, 1, 6, 0);
    step("N=1 again", make_frame(3), true, 1, 6, 0);

    // 3. N = 3, slots = 6: warm-up 1,2,3 then a sliding window, wrapping the 6-slot ring
    for (int k = 0; k < 9; k++) step("N=3 slots=6", make_frame(10 + k), true, 3, 6, 0);

    // 4. N = 5: changing N restarts the window
    for (int k = 0; k < 8; k++) step("N=5 slots=6", make_frame(30 + k), true, 5, 6, 0);

    // 5. N = slots: the frame being overwritten is the one leaving the window
    for (int k = 0; k < 9; k++) step("N=6=slots", make_frame(50 + k), true, 6, 6, 0);

    // 6. a constant picture must come out exactly the same however long the window
    for (int k = 0; k < 7; k++) step("constant picture N=4", make_const(0x40A0E0), true, 4, 8, 0);

    // 7. N larger than the ring is clamped to the ring size
    for (int k = 0; k < 6; k++) step("N=9 clamped to slots=4", make_frame(70 + k), true, 9, 4, 0);

    // 8. disable, then enable: the history is gone, the first frame comes out unchanged
    step("disabled again", make_frame(80), false, 4, 6, 0);
    step("re-enabled N=3 (restart)", make_frame(81), true, 3, 6, 0);
    step("N=3 second frame", make_frame(82), true, 3, 6, 0);

    // 9. the restart counter empties the window without changing anything else
    step("N=3 third frame", make_frame(83), true, 3, 6, 0);
    step("restart counter changed", make_frame(84), true, 3, 6, 1);
    step("after restart", make_frame(85), true, 3, 6, 1);

    // 10. the largest sum: 255 frames of 255 would be 65025 in 16 bits; here N = 8 of full white
    for (int k = 0; k < 10; k++) step("white N=8", make_const(0xFFFFFF), true, 8, 8, 0);

    if (fails) { printf("TEST FAILED (%d)\n", fails); return 1; }
    printf("TEST PASSED\n");
    return 0;
}
