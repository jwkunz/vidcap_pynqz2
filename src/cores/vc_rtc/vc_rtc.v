// vc_rtc.v - Free-running date/time clock with AXI4-Lite control.
//
// Counts calendar time from `aclk` (cycles-per-second is a register, so software can
// trim drift). Powers up at 2026-01-01 00:00:00 (REQ-TS-6). Provides the 14 decimal
// digits "CCYYMMDDHHMMSS" to the timestamp overlay.
//
// Register map (byte offsets, 32-bit):
//   0x00 ID        R    0x52544301 ("RTC", version 1)
//   0x04 CTRL      W    bit0 LOAD    : copy shadow regs into the live clock (rejected if invalid)
//                       bit1 LATCH   : capture live clock into the read-back regs
//                       bit2 CLR_ERR : clear STATUS.ERR
//   0x08 STATUS    R    bit0 VALID (software has loaded a time since reset)
//                       bit1 ERR   (last LOAD was rejected)
//   0x0C DATE      RW   write = shadow, read = latched copy
//                       [6:0] yy (0-99)  [14:8] century (0-99)  [19:16] month (1-12)  [28:24] day (1-31)
//   0x10 TIME      RW   write = shadow, read = latched copy
//                       [4:0] hour (0-23)  [13:8] minute (0-59)  [21:16] second (0-59)
//   0x14 CPS       RW   clock cycles per second (default CLK_HZ); trim to correct drift
//   0x18 SUBSEC    R    clock cycles elapsed in the current second (live)
`timescale 1ns/1ps
module vc_rtc #(
    parameter integer CLK_HZ = 100_000_000
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn, FREQ_HZ 100000000" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,

    input  wire [5:0]  s_axi_awaddr,
    input  wire        s_axi_awvalid,
    output wire        s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output wire        s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output wire        s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [5:0]  s_axi_araddr,
    input  wire        s_axi_arvalid,
    output wire        s_axi_arready,
    output wire [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output wire        s_axi_rvalid,
    input  wire        s_axi_rready,

    output wire [55:0] time_digits,   // {C1,C0,Y1,Y0,M1,M0,D1,D0,H1,H0,N1,N0,S1,S0}, 4 bits each
    output wire        time_valid
);
    localparam [31:0] ID = 32'h5254_4301;

    // ---- live clock ----
    reg [6:0] cc, yy;
    reg [3:0] mon;
    reg [4:0] day, hh;
    reg [5:0] mi, ss;
    reg [31:0] cps, sub;
    reg        valid, err;

    // ---- shadow + latched copies ----
    reg [6:0] s_cc, s_yy, l_cc, l_yy;
    reg [3:0] s_mon, l_mon;
    reg [4:0] s_day, s_hh, l_day, l_hh;
    reg [5:0] s_mi, s_ss, l_mi, l_ss;

    // leap year: yy != 0 ? yy%4==0 : century%4==0   (yy%4 == (2*tens + units)%4)
    function leap_of(input [6:0] c, input [6:0] y);
        reg [3:0] tens, units;
        begin
            tens  = y / 10;
            units = y % 10;
            if (y != 0) leap_of = (((tens << 1) + units) & 4'd3) == 0;
            else        leap_of = (c[1:0] == 2'd0);   // century divisible by 4
        end
    endfunction

    function [4:0] dim(input [3:0] m, input lp);
        case (m)
            4'd1, 4'd3, 4'd5, 4'd7, 4'd8, 4'd10, 4'd12: dim = 5'd31;
            4'd4, 4'd6, 4'd9, 4'd11:                    dim = 5'd30;
            default:                                    dim = lp ? 5'd29 : 5'd28;
        endcase
    endfunction

    wire        leap_live = leap_of(cc, yy);
    wire [4:0]  dim_live  = dim(mon, leap_live);
    wire        leap_sh   = leap_of(s_cc, s_yy);

    wire sh_ok = (s_mon >= 4'd1) && (s_mon <= 4'd12) &&
                 (s_day >= 5'd1) && (s_day <= dim(s_mon, leap_sh)) &&
                 (s_hh < 5'd24) && (s_mi < 6'd60) && (s_ss < 6'd60) &&
                 (s_cc < 7'd100) && (s_yy < 7'd100);

    // ---- register bus ----
    wire        wr_en;
    wire [5:0]  wr_addr, rd_addr;
    wire [31:0] wr_data;
    wire [3:0]  wr_strb;
    reg  [31:0] rd_data;

    vc_axil_bridge #(.ADDR_W(6)) u_bridge (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(s_axi_awaddr), .s_axi_awvalid(s_axi_awvalid), .s_axi_awready(s_axi_awready),
        .s_axi_wdata(s_axi_wdata), .s_axi_wstrb(s_axi_wstrb), .s_axi_wvalid(s_axi_wvalid),
        .s_axi_wready(s_axi_wready), .s_axi_bresp(s_axi_bresp), .s_axi_bvalid(s_axi_bvalid),
        .s_axi_bready(s_axi_bready), .s_axi_araddr(s_axi_araddr), .s_axi_arvalid(s_axi_arvalid),
        .s_axi_arready(s_axi_arready), .s_axi_rdata(s_axi_rdata), .s_axi_rresp(s_axi_rresp),
        .s_axi_rvalid(s_axi_rvalid), .s_axi_rready(s_axi_rready),
        .wr_en(wr_en), .wr_addr(wr_addr), .wr_data(wr_data), .wr_strb(wr_strb),
        .rd_addr(rd_addr), .rd_data(rd_data));

    always @* begin
        case (rd_addr[5:2])
            4'h0:    rd_data = ID;
            4'h1:    rd_data = 32'd0;
            4'h2:    rd_data = {30'd0, err, valid};
            4'h3:    rd_data = {3'd0, l_day, 4'd0, l_mon, 1'b0, l_cc, 1'b0, l_yy};
            4'h4:    rd_data = {10'd0, l_ss, 2'd0, l_mi, 3'd0, l_hh};
            4'h5:    rd_data = cps;
            4'h6:    rd_data = sub;
            default: rd_data = 32'd0;
        endcase
    end

    wire ctl_wr = wr_en && (wr_addr[5:2] == 4'h1) && wr_strb[0];
    wire do_load  = ctl_wr && wr_data[0];
    wire do_latch = ctl_wr && wr_data[1];
    wire do_clr   = ctl_wr && wr_data[2];

    // ---- tick ----
    wire tick = (sub >= cps - 1);

    always @(posedge aclk) begin
        if (!aresetn) begin
            cc <= 7'd20; yy <= 7'd26; mon <= 4'd1; day <= 5'd1;
            hh <= 5'd0;  mi <= 6'd0;  ss <= 6'd0;
            cps <= CLK_HZ; sub <= 32'd0; valid <= 1'b0; err <= 1'b0;
            s_cc <= 7'd20; s_yy <= 7'd26; s_mon <= 4'd1; s_day <= 5'd1;
            s_hh <= 5'd0;  s_mi <= 6'd0;  s_ss <= 6'd0;
            l_cc <= 7'd20; l_yy <= 7'd26; l_mon <= 4'd1; l_day <= 5'd1;
            l_hh <= 5'd0;  l_mi <= 6'd0;  l_ss <= 6'd0;
        end else begin
            // shadow writes
            if (wr_en && wr_addr[5:2] == 4'h3) begin
                if (wr_strb[0]) s_yy  <= wr_data[6:0];
                if (wr_strb[1]) s_cc  <= wr_data[14:8];
                if (wr_strb[2]) s_mon <= wr_data[19:16];
                if (wr_strb[3]) s_day <= wr_data[28:24];
            end
            if (wr_en && wr_addr[5:2] == 4'h4) begin
                if (wr_strb[0]) s_hh <= wr_data[4:0];
                if (wr_strb[1]) s_mi <= wr_data[13:8];
                if (wr_strb[2]) s_ss <= wr_data[21:16];
            end
            if (wr_en && wr_addr[5:2] == 4'h5 && (&wr_strb) && wr_data != 32'd0) cps <= wr_data;

            if (do_clr) err <= 1'b0;

            if (do_latch) begin
                l_cc <= cc; l_yy <= yy; l_mon <= mon; l_day <= day;
                l_hh <= hh; l_mi <= mi;  l_ss <= ss;
            end

            // time advance / load
            if (do_load) begin
                if (sh_ok) begin
                    cc <= s_cc; yy <= s_yy; mon <= s_mon; day <= s_day;
                    hh <= s_hh; mi <= s_mi; ss <= s_ss;
                    sub <= 32'd0; valid <= 1'b1; err <= 1'b0;
                end else begin
                    err <= 1'b1;
                end
            end else if (tick) begin
                sub <= 32'd0;
                if (ss != 6'd59) ss <= ss + 6'd1;
                else begin
                    ss <= 6'd0;
                    if (mi != 6'd59) mi <= mi + 6'd1;
                    else begin
                        mi <= 6'd0;
                        if (hh != 5'd23) hh <= hh + 5'd1;
                        else begin
                            hh <= 5'd0;
                            if (day != dim_live) day <= day + 5'd1;
                            else begin
                                day <= 5'd1;
                                if (mon != 4'd12) mon <= mon + 4'd1;
                                else begin
                                    mon <= 4'd1;
                                    if (yy != 7'd99) yy <= yy + 7'd1;
                                    else begin
                                        yy <= 7'd0;
                                        cc <= (cc == 7'd99) ? 7'd0 : cc + 7'd1;
                                    end
                                end
                            end
                        end
                    end
                end
            end else begin
                sub <= sub + 32'd1;
            end
        end
    end

    // ---- BCD digits for the overlay ----
    function [7:0] bcd2(input [6:0] v);
        reg [3:0] t;
        reg [6:0] u;
        begin
            t = v / 10;
            u = v - (t * 10);
            bcd2 = {t, u[3:0]};
        end
    endfunction
    wire [7:0] b_cc = bcd2(cc), b_yy = bcd2(yy), b_mo = bcd2({3'd0, mon}),
               b_dd = bcd2({2'd0, day}), b_hh = bcd2({2'd0, hh}),
               b_mi = bcd2({1'b0, mi}), b_ss = bcd2({1'b0, ss});
    assign time_digits = {b_cc, b_yy, b_mo, b_dd, b_hh, b_mi, b_ss};
    assign time_valid  = valid;
endmodule
