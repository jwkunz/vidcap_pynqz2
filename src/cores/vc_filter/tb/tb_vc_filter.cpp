// C-sim / co-sim testbench for vc_filter: compares the streaming core against a plain
// frame-at-a-time golden model for every mode.
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <vector>
#include <algorithm>
#include "../vc_filter.h"

typedef std::vector<uint32_t> Frame;   // 0x00RRGGBB per pixel

static int R(uint32_t p) { return (p >> 16) & 255; }
static int G(uint32_t p) { return (p >> 8) & 255; }
static int B(uint32_t p) { return p & 255; }
static uint32_t RGB(int r, int g, int b) { return ((uint32_t)r << 16) | ((uint32_t)g << 8) | (uint32_t)b; }
static int luma(uint32_t p) { return (77 * R(p) + 150 * G(p) + 29 * B(p)) >> 8; }
static int clampi(int v) { return v < 0 ? 0 : (v > 255 ? 255 : v); }

static uint32_t at(const Frame &f, int x, int y) {
    x = std::max(0, std::min(VC_WIDTH - 1, x));
    y = std::max(0, std::min(VC_HEIGHT - 1, y));
    return f[y * VC_WIDTH + x];
}

static Frame golden(const Frame &in, int mode, int thr) {
    Frame out(in.size());
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            uint32_t c = at(in, x, y), res = c;
            switch (mode) {
            case VC_MODE_INVERT: res = ~c & 0xFFFFFF; break;
            case VC_MODE_GRAYSCALE: { int g = luma(c); res = RGB(g, g, g); break; }
            case VC_MODE_THRESHOLD: res = luma(c) > thr ? 0xFFFFFF : 0; break;
            case VC_MODE_SOBEL: {
                int gx = 0, gy = 0;
                static const int kx[3][3] = {{-1, 0, 1}, {-2, 0, 2}, {-1, 0, 1}};
                static const int ky[3][3] = {{-1, -2, -1}, {0, 0, 0}, {1, 2, 1}};
                for (int j = -1; j <= 1; j++)
                    for (int i = -1; i <= 1; i++) {
                        int l = luma(at(in, x + i, y + j));
                        gx += kx[j + 1][i + 1] * l;
                        gy += ky[j + 1][i + 1] * l;
                    }
                int m = clampi(std::abs(gx) + std::abs(gy));
                res = RGB(m, m, m);
                break;
            }
            case VC_MODE_BLUR: {
                static const int k[3][3] = {{1, 2, 1}, {2, 4, 2}, {1, 2, 1}};
                int s[3] = {8, 8, 8};
                for (int j = -1; j <= 1; j++)
                    for (int i = -1; i <= 1; i++) {
                        uint32_t p = at(in, x + i, y + j);
                        s[0] += k[j + 1][i + 1] * R(p); s[1] += k[j + 1][i + 1] * G(p); s[2] += k[j + 1][i + 1] * B(p);
                    }
                res = RGB(s[0] >> 4, s[1] >> 4, s[2] >> 4);
                break;
            }
            case VC_MODE_SHARPEN: {
                uint32_t u = at(in, x, y - 1), d = at(in, x, y + 1), l = at(in, x - 1, y), r = at(in, x + 1, y);
                res = RGB(clampi(5 * R(c) - R(u) - R(d) - R(l) - R(r)),
                          clampi(5 * G(c) - G(u) - G(d) - G(l) - G(r)),
                          clampi(5 * B(c) - B(u) - B(d) - B(l) - B(r)));
                break;
            }
            default: break;   // passthrough
            }
            out[y * VC_WIDTH + x] = res;
        }
    return out;
}

static void push_frame(vc_stream &s, const Frame &f) {
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            vc_beat b;
            b.data = f[y * VC_WIDTH + x];
            b.user = (x == 0 && y == 0);
            b.last = (x == VC_WIDTH - 1);
            s.write(b);
        }
}

static int check_frame(vc_stream &s, const Frame &exp, const char *tag) {
    int bad = 0, struct_bad = 0;
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            if (s.empty()) { printf("FAIL %s: stream ended early at (%d,%d)\n", tag, x, y); return 1; }
            vc_beat b = s.read();
            if ((int)b.user != (x == 0 && y == 0) || (int)b.last != (x == VC_WIDTH - 1)) struct_bad++;
            if ((uint32_t)b.data != exp[y * VC_WIDTH + x]) {
                if (bad < 3) printf("  %s mismatch at (%d,%d): got %06x expected %06x\n", tag, x, y, (uint32_t)b.data, exp[y * VC_WIDTH + x]);
                bad++;
            }
        }
    if (!s.empty()) { printf("FAIL %s: extra output beats\n", tag); return 1; }
    if (bad || struct_bad) { printf("FAIL %s: %d pixel mismatches, %d framing errors\n", tag, bad, struct_bad); return 1; }
    printf("ok   %s\n", tag);
    return 0;
}

static Frame make_noise(uint32_t seed) {
    Frame f(VC_WIDTH * VC_HEIGHT);
    for (auto &p : f) { seed = seed * 1664525u + 1013904223u; p = (seed >> 8) & 0xFFFFFF; }
    return f;
}
static Frame make_shapes() {
    Frame f(VC_WIDTH * VC_HEIGHT);
    for (int y = 0; y < VC_HEIGHT; y++)
        for (int x = 0; x < VC_WIDTH; x++) {
            int r = (x * 255) / (VC_WIDTH - 1), g = (y * 255) / (VC_HEIGHT - 1), b = 40;
            if (x > VC_WIDTH / 4 && x < VC_WIDTH / 2 && y > VC_HEIGHT / 4 && y < 3 * VC_HEIGHT / 4) { r = 250; g = 250; b = 250; }
            if (x == 0 || y == 0 || x == VC_WIDTH - 1 || y == VC_HEIGHT - 1) { r = 255; g = 0; b = 0; }   // border colours
            f[y * VC_WIDTH + x] = RGB(r, g, b);
        }
    return f;
}

int main() {
    static const char *names[] = {"passthrough", "invert", "grayscale", "threshold", "sobel", "blur", "sharpen"};
    int fails = 0;
    Frame imgs[2] = {make_noise(12345), make_shapes()};
    const int THR = 100;

    for (int im = 0; im < 2; im++)
        for (int mode = 0; mode < VC_MODE_COUNT; mode++) {
            vc_stream in, out;
            // leading junk before the start of frame must be ignored
            for (int k = 0; k < VC_WIDTH / 2 + 3; k++) { vc_beat j; j.data = 0x123456 + k; j.user = 0; j.last = 0; in.write(j); }
            push_frame(in, imgs[im]);
            vc_filter(in, out, mode, THR);
            char tag[64]; snprintf(tag, sizeof tag, "%s / %s", im ? "shapes" : "noise", names[mode]);
            fails += check_frame(out, golden(imgs[im], mode, THR), tag);
        }

    // back-to-back frames with different modes (mode is re-read each frame)
    {
        vc_stream in, out;
        push_frame(in, imgs[1]); push_frame(in, imgs[0]);
        vc_filter(in, out, VC_MODE_SOBEL, THR);
        fails += check_frame(out, golden(imgs[1], VC_MODE_SOBEL, THR), "two frames: first (sobel)");
        vc_filter(in, out, VC_MODE_INVERT, THR);
        fails += check_frame(out, golden(imgs[0], VC_MODE_INVERT, THR), "two frames: second (invert)");
    }
    // the unused mode code 7 must behave like passthrough
    {
        vc_stream in, out;
        push_frame(in, imgs[0]);
        vc_filter(in, out, 7, THR);
        fails += check_frame(out, golden(imgs[0], 0, THR), "mode 7 == passthrough");
    }

    if (fails) { printf("TEST FAILED (%d)\n", fails); return 1; }
    printf("TEST PASSED\n");
    return 0;
}
