// vc_lut3d.v - 3D colour look-up table (colour grading) for a 24-bit {R,G,B} AXI4-Stream.
//
// A 17x17x17 colour cube with trilinear interpolation: each output colour is read from the 8 cube nodes
// around the input colour and blended. Software loads the cube (any "look") through the register window and
// can fade it in and out with STRENGTH. One pixel per clock, fixed latency, back-pressure safe.
//
// Storage: the 8 corners of any cell differ in the parity of every coordinate, so the cube is split into 8
// banks by node parity {r[0],g[0],b[0]}. Every bank is read once per pixel, so the 8 corners come out in a
// single clock and the whole cube needs only 8 small block RAMs.
//   node (r,g,b), 0..16 each:   bank = {r[0],g[0],b[0]},  offset = (r>>1) + 9*(g>>1) + 81*(b>>1)   (0..728)
//
// Interpolation (all integer, exactly mirrored by vidcap/lut.py, which the testbench is checked against):
//   p = v*257 (16 bits): node index i = v[7:4], fraction f = {v[3:0],v[7:4]} (8 bits; v=255 lands on the last node)
//   lerp8(a,b,f)  = a*256 + (b-a)*f                      (R axis, 8-bit nodes -> 16 bits)
//   lerp16(a,b,f) = (a*256 + (b-a)*f) >> 8               (G then B axis)
//   graded = (result + 128) >> 8
//   out    = clamp(orig + (((graded - orig) * strength + 128) >>> 8))       strength 0..256, 256 = full, 0 = off
//
// Register map (byte offsets, 64 KiB window):
//   0x0000 ID        R   0x4C555403
//   0x0004 CTRL      RW  bit0 ENABLE (0 = bypass, default 0)   bit1 CLEAR (write 1: clear WRITES and CHECKSUM)
//   0x0008 STRENGTH  RW  0..256 (default 256). ENABLE and STRENGTH take effect at the next frame.
//   0x000C FRAMES    R   frames accepted
//   0x0010 WRITES    R   cube words written since CLEAR
//   0x0014 CHECKSUM  R   sum (mod 2^32) of those words, to verify a load
//   0x0018 PROBE_IN  R   {8'b0,R,G,B} first pixel of the latest frame, before the table
//   0x001C PROBE_OUT R   the same pixel after the table (same frame once the picture is static)
//   0x8000 CUBE      W   8 banks x 4 KiB: word address 0x8000 + bank*0x1000 + offset*4, data {8'b0,R,G,B}.
//                        Write only, full 32-bit words. The cube holds an identity table after configuration.
`timescale 1ns/1ps
module vc_lut3d (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi:s_axis:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 100000000" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,

    input  wire [15:0] s_axi_awaddr,  input  wire s_axi_awvalid, output wire s_axi_awready,
    input  wire [31:0] s_axi_wdata,   input  wire [3:0] s_axi_wstrb,
    input  wire        s_axi_wvalid,  output wire s_axi_wready,
    output wire [1:0]  s_axi_bresp,   output wire s_axi_bvalid,  input  wire s_axi_bready,
    input  wire [15:0] s_axi_araddr,  input  wire s_axi_arvalid, output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,   output wire [1:0] s_axi_rresp,
    output wire        s_axi_rvalid,  input  wire s_axi_rready,

    input  wire [23:0] s_axis_tdata,  input  wire s_axis_tvalid, output wire s_axis_tready,
    input  wire        s_axis_tuser,  input  wire s_axis_tlast,
    output wire [23:0] m_axis_tdata,  output wire m_axis_tvalid, input  wire m_axis_tready,
    output wire        m_axis_tuser,  output wire m_axis_tlast
);
    localparam [31:0] ID = 32'h4C55_5403;

    // ------------------------------------------------------------------ registers
    wire        wr_en;  wire [15:0] wr_addr, rd_addr;  wire [31:0] wr_data;  wire [3:0] wr_strb;
    reg  [31:0] rd_data;
    reg         ctrl_en;
    reg  [8:0]  strength;
    reg  [31:0] frames, writes, checksum;
    reg  [23:0] probe_in, probe_out;

    vc_axil_bridge #(.ADDR_W(16)) u_bridge (
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
        case (rd_addr[4:2])
            3'd0:    rd_data = ID;
            3'd1:    rd_data = {31'd0, ctrl_en};
            3'd2:    rd_data = {23'd0, strength};
            3'd3:    rd_data = frames;
            3'd4:    rd_data = writes;
            3'd5:    rd_data = checksum;
            3'd6:    rd_data = {8'd0, probe_in};
            default: rd_data = {8'd0, probe_out};
        endcase
        if (rd_addr[15]) rd_data = 32'd0;                   // the cube is write-only
    end

    wire cube_we = wr_en && wr_addr[15];
    wire reg_we  = wr_en && !wr_addr[15];
    always @(posedge aclk) begin
        if (!aresetn) begin
            ctrl_en <= 1'b0; strength <= 9'd256; writes <= 32'd0; checksum <= 32'd0;
        end else begin
            if (reg_we) begin
                case (wr_addr[4:2])
                    3'd1: begin
                        ctrl_en <= wr_data[0];
                        if (wr_data[1]) begin writes <= 32'd0; checksum <= 32'd0; end
                    end
                    3'd2: strength <= (wr_data[8:0] > 9'd256) ? 9'd256 : wr_data[8:0];
                    default: ;
                endcase
            end
            if (cube_we) begin
                writes   <= writes + 32'd1;
                checksum <= checksum + {8'd0, wr_data[23:0]};
            end
        end
    end

    // ------------------------------------------------------------------ pipeline
    // One enable moves every stage: the pipeline holds still while the output is blocked. Stage k of the valid
    // vector V and of the side-band M = {pixel, strength, tuser, tlast} describes the data held in stage k.
    //   0 capture   1 addresses   2 banks   3 corners   4/5 R axis (difference, multiply-add)   6/7 G axis
    //   8/9 B axis   10 round   11/12/13 blend (difference, scale, add and clamp)           latency 14 clocks
    localparam LAST = 13;
    reg  [LAST:0] V;
    reg  [34:0]   M0, M1, M2, M3, M4, M5, M6, M7, M8, M9, M10, M11, M12, M13;
    wire adv = m_axis_tready || !V[LAST];
    assign s_axis_tready = adv;

    // ---- stage 0: capture the pixel; ENABLE/STRENGTH are sampled on the first pixel of a frame
    reg  [8:0]  str_q;
    wire        sof_in = s_axis_tvalid && s_axis_tuser;
    wire [8:0]  str_in = sof_in ? (ctrl_en ? strength : 9'd0) : str_q;
    always @(posedge aclk) begin
        if (!aresetn) begin
            V[LAST-1:0] <= {LAST{1'b0}};
            str_q <= 9'd0; frames <= 32'd0; probe_in <= 24'd0;
        end else if (adv) begin
            V[0] <= s_axis_tvalid;
            M0 <= {s_axis_tdata, str_in, s_axis_tuser, s_axis_tlast};
            if (s_axis_tvalid) str_q <= str_in;
            if (sof_in) begin frames <= frames + 32'd1; probe_in <= s_axis_tdata; end
            V[LAST-1:1] <= V[LAST-2:0];
            M1 <= M0; M2 <= M1; M3 <= M2; M4 <= M3; M5 <= M4; M6 <= M5; M7 <= M6;
            M8 <= M7; M9 <= M8; M10 <= M9; M11 <= M10; M12 <= M11;
        end
    end

    // ---- stage 1: node indices, fractions, and the address in each bank
    wire [23:0] px0 = M0[34:11];
    wire [3:0]  ri = px0[23:20], gi = px0[15:12], bi = px0[7:4];
    reg  [23:0] F1, F2, F3, F4, F5, F6, F7, F8;             // fractions {fr, fg, fb}, kept alongside the data
    reg  [2:0]  par1, par2;
    reg  [79:0] addr1;                                      // 8 x 10 bits

    function [9:0] bank_addr(input [3:0] r, input [3:0] g, input [3:0] b, input pr, input pg, input pb);
        reg [4:0] cr, cg, cb;
        reg [3:0] hr, hg, hb;
        begin
            cr = (r[0] == pr) ? {1'b0, r} : ({1'b0, r} + 5'd1);
            cg = (g[0] == pg) ? {1'b0, g} : ({1'b0, g} + 5'd1);
            cb = (b[0] == pb) ? {1'b0, b} : ({1'b0, b} + 5'd1);
            hr = cr[4:1]; hg = cg[4:1]; hb = cb[4:1];
            bank_addr = {6'd0, hr} + ({6'd0, hg} << 3) + {6'd0, hg} + ({6'd0, hb} << 6) + ({6'd0, hb} << 4) + {6'd0, hb};
        end
    endfunction

    integer q;
    always @(posedge aclk) if (adv) begin
        for (q = 0; q < 8; q = q + 1)
            addr1[10*q +: 10] <= bank_addr(ri, gi, bi, q[2], q[1], q[0]);
        F1  <= {px0[19:16], px0[23:20], px0[11:8], px0[15:12], px0[3:0], px0[7:4]};
        par1 <= {ri[0], gi[0], bi[0]};
    end

    // ---- stage 2: the 8 banks (block RAM, one read each)
    wire [191:0] rd_all;

    function [7:0] ident(input integer n);                  // identity table value at node n
        ident = (n > 16) ? 8'd255 : ((n * 255 + 8) / 16);
    endfunction

    genvar k;
    generate
        for (k = 0; k < 8; k = k + 1) begin : bank
            (* ram_style = "block" *) reg [23:0] mem [0:1023];
            reg [23:0] rdk;
            integer i, nr, ng, nb;
            initial begin
                for (i = 0; i < 1024; i = i + 1) begin
                    nr = 2 * (i % 9)        + ((k >> 2) & 1);
                    ng = 2 * ((i % 81) / 9) + ((k >> 1) & 1);
                    nb = 2 * (i / 81)       + (k & 1);
                    mem[i] = {ident(nr), ident(ng), ident(nb)};
                end
            end
            always @(posedge aclk) if (cube_we && wr_addr[14:12] == k) mem[wr_addr[11:2]] <= wr_data[23:0];
            always @(posedge aclk) if (adv) rdk <= mem[addr1[10*k +: 10]];
            assign rd_all[24*k +: 24] = rdk;
        end
    endgenerate
    always @(posedge aclk) if (adv) begin
        F2 <= F1; par2 <= par1;
    end

    // ---- stage 3: put the 8 corners in order c[{dr,dg,db}]
    reg [191:0] c3;
    integer j;
    always @(posedge aclk) if (adv) begin
        for (j = 0; j < 8; j = j + 1)
            c3[24*j +: 24] <= rd_all[24 * {par2[2] ^ j[2], par2[1] ^ j[1], par2[0] ^ j[0]} +: 24];
        F3 <= F2;
    end

    // ---- stages 4, 5: interpolate along R. 4 pairs {dg,db} x 3 channels. The difference b-a is registered
    //      first so that each stage is either an adder or a multiplier, never both.
    function [15:0] mac8(input [7:0] a, input signed [8:0] d, input [7:0] f);       // a*256 + d*f
        reg signed [26:0] t;
        begin
            t = $signed({1'b0, a, 8'd0}) + d * $signed({1'b0, f});
            mac8 = t[15:0];
        end
    endfunction
    function [15:0] mac16(input [15:0] a, input signed [16:0] d, input [7:0] f);    // (a*256 + d*f) >> 8
        reg signed [34:0] t;
        begin
            t = $signed({3'b0, a, 8'd0}) + d * $signed({1'b0, f});
            mac16 = t[23:8];
        end
    endfunction

    reg [107:0] d4;                                         // 12 x 9 bits, signed differences
    reg [95:0]  a4;                                         // 12 x 8 bits, the lower node
    reg [191:0] t5;                                         // 12 x 16 bits
    integer p, ch;
    always @(posedge aclk) if (adv) begin
        for (p = 0; p < 4; p = p + 1)
            for (ch = 0; ch < 3; ch = ch + 1) begin
                d4[9*(3*p+ch) +: 9] <= $signed({1'b0, c3[24*{1'b1, p[1:0]} + 8*ch +: 8]}) - $signed({1'b0, c3[24*{1'b0, p[1:0]} + 8*ch +: 8]});
                a4[8*(3*p+ch) +: 8] <= c3[24*{1'b0, p[1:0]} + 8*ch +: 8];
            end
        F4 <= F3;
    end
    always @(posedge aclk) if (adv) begin
        for (p = 0; p < 12; p = p + 1)
            t5[16*p +: 16] <= mac8(a4[8*p +: 8], d4[9*p +: 9], F4[23:16]);
        F5 <= F4;
    end

    // ---- stages 6, 7: along G (for each db, 3 channels). Inputs t5 index = 3*{dg,db} + ch.
    reg [101:0] d6;                                         // 6 x 17 bits
    reg [95:0]  a6;                                         // 6 x 16 bits
    reg [95:0]  t7;                                         // 6 x 16 bits, index 3*db + ch
    integer db;
    always @(posedge aclk) if (adv) begin
        for (db = 0; db < 2; db = db + 1)
            for (ch = 0; ch < 3; ch = ch + 1) begin
                d6[17*(3*db+ch) +: 17] <= $signed({1'b0, t5[16*(3*{1'b1, db[0]} + ch) +: 16]}) - $signed({1'b0, t5[16*(3*{1'b0, db[0]} + ch) +: 16]});
                a6[16*(3*db+ch) +: 16] <= t5[16*(3*{1'b0, db[0]} + ch) +: 16];
            end
        F6 <= F5;
    end
    always @(posedge aclk) if (adv) begin
        for (p = 0; p < 6; p = p + 1)
            t7[16*p +: 16] <= mac16(a6[16*p +: 16], d6[17*p +: 17], F6[15:8]);
        F7 <= F6;
    end

    // ---- stages 8, 9: along B
    reg [50:0] d8;                                          // 3 x 17 bits
    reg [47:0] a8;                                          // 3 x 16 bits
    reg [47:0] t9;
    always @(posedge aclk) if (adv) begin
        for (ch = 0; ch < 3; ch = ch + 1) begin
            d8[17*ch +: 17] <= $signed({1'b0, t7[16*(3 + ch) +: 16]}) - $signed({1'b0, t7[16*ch +: 16]});
            a8[16*ch +: 16] <= t7[16*ch +: 16];
        end
        F8 <= F7;
    end
    always @(posedge aclk) if (adv) begin
        for (ch = 0; ch < 3; ch = ch + 1)
            t9[16*ch +: 16] <= mac16(a8[16*ch +: 16], d8[17*ch +: 17], F8[7:0]);
    end

    // ---- stage 10: round to 8 bits
    reg [23:0] g10;
    always @(posedge aclk) if (adv) begin
        for (ch = 0; ch < 3; ch = ch + 1)
            g10[8*ch +: 8] <= (t9[16*ch +: 16] + 16'd128) >> 8;
    end

    // ---- stage 11: difference between the graded and the original colour
    wire [23:0] px10 = M10[34:11];
    reg [29:0] d11;                                         // 3 x 10 bits, signed
    always @(posedge aclk) if (adv) begin
        for (ch = 0; ch < 3; ch = ch + 1)
            d11[10*ch +: 10] <= $signed({2'b0, g10[8*ch +: 8]}) - $signed({2'b0, px10[8*ch +: 8]});
    end

    // ---- stage 12: scale the difference by STRENGTH
    wire [8:0] s11 = M11[10:2];
    reg [62:0] m12;                                         // 3 x 21 bits, signed
    always @(posedge aclk) if (adv) begin
        for (ch = 0; ch < 3; ch = ch + 1)
            m12[21*ch +: 21] <= $signed(d11[10*ch +: 10]) * $signed({1'b0, s11}) + 21'sd128;
    end

    // ---- stage 13: add back to the original, clamp
    function [7:0] addclamp(input [7:0] orig, input signed [20:0] m);
        reg signed [20:0] r;
        begin
            r = $signed({13'b0, orig}) + (m >>> 8);
            addclamp = (r < 0) ? 8'd0 : ((r > 255) ? 8'd255 : r[7:0]);
        end
    endfunction

    wire [23:0] px12 = M12[34:11];
    reg  [23:0] out13;
    wire [23:0] blended = {addclamp(px12[23:16], m12[62:42]), addclamp(px12[15:8], m12[41:21]), addclamp(px12[7:0], m12[20:0])};
    always @(posedge aclk) begin
        if (!aresetn) begin probe_out <= 24'd0; V[LAST] <= 1'b0; end
        else if (adv) begin
            V[LAST] <= V[LAST-1]; M13 <= M12; out13 <= blended;
            if (V[LAST-1] && M12[1]) probe_out <= blended;
        end
    end

    assign m_axis_tdata  = out13;
    assign m_axis_tvalid = V[LAST];
    assign m_axis_tuser  = M13[1];
    assign m_axis_tlast  = M13[0];
endmodule
