// vc_overlay.h - Date/time text overlay (REQ-TS-*).
#ifndef VC_OVERLAY_H
#define VC_OVERLAY_H
#include "vc_video.h"

// Text is "CCYY-MM-DD HH:MM:SS" (19 characters of 8x16 pixels, times `scale`), drawn in the
// bottom-right corner, `margin` pixels in from the edges.
#define VC_OVERLAY_CHARS 19
#define VC_GLYPH_W 8
#define VC_GLYPH_H 16

// time_digits: 14 BCD nibbles {C1,C0,Y1,Y0,M1,M0,D1,D0,H1,H0,N1,N0,S1,S0}, most significant first
//              (straight from vc_rtc). Sampled once per frame.
// enable       0 = pass the video through untouched
// scale        1..4 (0 is treated as 1, larger values as 4)
// margin       distance from the right and bottom edges, in pixels
// fg / bg      {R,G,B} text and background colours; bg is used only when bg_en = 1
void vc_overlay(vc_stream &in, vc_stream &out,
                ap_uint<56> time_digits,
                ap_uint<1> enable, ap_uint<3> scale, ap_uint<8> margin,
                ap_uint<24> fg, ap_uint<24> bg, ap_uint<1> bg_en);

#endif
