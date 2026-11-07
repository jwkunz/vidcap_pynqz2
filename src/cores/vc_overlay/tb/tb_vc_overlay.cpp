// C-sim / co-sim testbench for vc_overlay: renders the text with a plain per-pixel golden
// routine and compares whole frames.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include "../vc_overlay.h"
#include "../vc_font.h"

typedef std::vector<uint32_t> Frame;

// "20261107094103" -> 56-bit BCD value
static ap_uint<56> bcd(const char *digits) {
    ap_uint<56> v = 0;
    for (int k = 0; k < 14; k++) {
        char c = digits[k];
        v.range(55 - 4 * k, 52 - 4 * k) = (c >= 'a') ? (10 + c - 'a') : (c - '0');
    }
    return v;
}

static Frame golden(const Frame &in, const char *digits, bool en, int scale, int margin,
                    uint32_t fg, uint32_t bg, bool bg_en) {
    Frame out = in;
    // build the string "CCYY-MM-DD HH:MM:SS"
    char text[VC_OVERLAY_CHARS + 1];
    snprintf(text, sizeof text, "%.4s-%.2s-%.2s %.2s:%.2s:%.2s", digits, digits + 4, digits + 6,
             digits + 8, digits + 10, digits + 12);
    int s = scale == 0 ? 1 : (scale > 4 ? 4 : scale);
    int bw = VC_OVERLAY_CHARS * 8 * s, bh = 16 * s;
    int tx0 = VC_WIDTH - margin - bw, ty0 = VC_HEIGHT - margin - bh;
    if (!en || tx0 < 0 || ty0 < 0) return out;
    for (int y = ty0; y < ty0 + bh; y++)
        for (int x = tx0; x < tx0 + bw; x++) {
            int ci = (x - tx0) / (8 * s), px = ((x - tx0) / s) % 8, py = (y - ty0) / s;
            char ch = text[ci];
            int g = (ch >= '0' && ch <= '9') ? ch - '0' : ch == '-' ? 10 : ch == ':' ? 11 : 12;
            bool on = (vc_font[g][py] >> (7 - px)) & 1;
            if (on) out[y * VC_WIDTH + x] = fg;
            else if (bg_en) out[y * VC_WIDTH + x] = bg;
        }
    return out;
}

static Frame make_frame() {
    Frame f(VC_WIDTH * VC_HEIGHT);
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++)
            f[y * VC_WIDTH + x] = ((x * 3 + y) & 0xFF) << 16 | ((x + y * 5) & 0xFF) << 8 | ((x ^ y) & 0xFF);
    return f;
}

static int run_case(const char *name, const char *digits, bool en, int scale, int margin,
                    uint32_t fg, uint32_t bg, bool bg_en) {
    Frame in = make_frame();
    vc_stream si, so;
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            vc_beat b; b.data = in[y * VC_WIDTH + x]; b.user = (x == 0 && y == 0); b.last = (x == VC_WIDTH - 1);
            si.write(b);
        }
    vc_overlay(si, so, bcd(digits), en, scale, margin, fg, bg, bg_en);
    Frame exp = golden(in, digits, en, scale, margin, fg, bg, bg_en);
    int bad = 0, sbad = 0, changed = 0;
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            if (so.empty()) { printf("FAIL %s: output ended early\n", name); return 1; }
            vc_beat b = so.read();
            if ((int)b.user != (x == 0 && y == 0) || (int)b.last != (x == VC_WIDTH - 1)) sbad++;
            if ((uint32_t)b.data != exp[y * VC_WIDTH + x]) bad++;
            if ((uint32_t)b.data != in[y * VC_WIDTH + x]) changed++;
        }
    if (!so.empty()) { printf("FAIL %s: extra output beats\n", name); return 1; }
    if (bad || sbad) { printf("FAIL %s: %d pixel mismatches, %d framing errors\n", name, bad, sbad); return 1; }
    printf("ok   %s (%d pixels changed)\n", name, changed);
    return 0;
}

int main() {
    int fails = 0;
    fails += run_case("disabled: video untouched", "20261107094103", false, 1, 4, 0xFFFFFF, 0, true);
    fails += run_case("scale 1, white on black", "20261107094103", true, 1, 4, 0xFFFFFF, 0x000000, true);
    fails += run_case("scale 1, transparent background", "20261107094103", true, 1, 4, 0xFFFF00, 0, false);
    fails += run_case("scale 2", "20990231235959", true, 2, 4, 0x00FF00, 0x000040, true);
    fails += run_case("scale 2, odd margin", "20260101000000", true, 2, 5, 0xFF8000, 0, false);
    fails += run_case("scale 0 acts as 1", "20260101000000", true, 0, 4, 0xFFFFFF, 0, true);
    fails += run_case("scale 7 acts as 4 (does not fit -> untouched)", "20260101000000", true, 7, 4, 0xFFFFFF, 0, true);
    fails += run_case("scale 3 (does not fit -> untouched)", "20261107094103", true, 3, 2, 0x00FFFF, 0x202020, true);
    fails += run_case("larger margin", "19700101000000", true, 1, 10, 0xFF0000, 0, false);
    fails += run_case("invalid BCD digit shows a space", "20ff1107094103", true, 1, 4, 0xFFFFFF, 0, true);
    if (fails) { printf("TEST FAILED (%d)\n", fails); return 1; }
    printf("TEST PASSED\n");
    return 0;
}
