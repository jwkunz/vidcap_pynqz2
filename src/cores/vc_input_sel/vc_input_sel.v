// vc_input_sel.v - Input source select, frame normaliser, HDMI monitor, test-pattern pacer.
//
// Two AXI4-Stream video inputs (HDMI from v_vid_in_axi4s, and a test-pattern generator)
// feed one output stream that is ALWAYS made of complete W x H frames:
//   * the source is switched only at a frame boundary;
//   * a short/long/aborted source frame is finished with black pixels, so downstream
//     (VDMA) never sees a malformed frame;
//   * HDMI frames are forwarded only while the HDMI input is "good" (locked, W x H, frames
//     arriving); the test pattern is paced to one frame per TPG_PERIOD clocks.
// If the source is HDMI and the input has not been good for TIMEOUT clocks, the design
// falls back to the test pattern, and returns after the input has been good for RETURN clocks.
//
// Stream format (both inputs and the output): tdata[23:0] pixel, tuser = start of frame,
// tlast = end of line.  All on one clock `aclk`.
//
// Register map (byte offsets):
//   0x00 ID         R   0x49534C01
//   0x04 CTRL       RW  bit0 SRC (0 = HDMI, 1 = test pattern)   bit1 FALLBACK_EN (default 1)
//   0x08 STATUS     R   bit0 HDMI_LOCKED  bit1 HDMI_GOOD  bit2 FALLBACK_ACTIVE
//                       bit3 ACTIVE_SRC (0 = HDMI, 1 = TPG)  bit4 FRAME_ABORT (sticky, write 1 to clear)
//   0x0C TIMEOUT    RW  clocks of bad HDMI before fallback (default 5 s)
//   0x10 RETURN     RW  clocks of good HDMI before leaving fallback (default 1 s)
//   0x14 MEASURED   R   [15:0] width, [31:16] height of the last HDMI frame
//   0x18 HDMI_FRAMES R  count of HDMI frames seen
//   0x1C OUT_FRAMES  R  count of frames emitted
//   0x20 TPG_PERIOD RW  clocks per test-pattern frame (default CLK_HZ/60)
`timescale 1ns/1ps
module vc_input_sel #(
    parameter integer W            = 1280,
    parameter integer H            = 720,
    parameter integer CLK_HZ       = 100_000_000,
    parameter integer WATCHDOG     = 5_000_000,   // clocks without an HDMI SOF => not good
    parameter integer STALL_LIMIT  = 2_000_000    // clocks without a beat inside a frame => abort/pad
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi:s_axis_hdmi:s_axis_tpg:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 100000000" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,

    // register interface
    input  wire [5:0]  s_axi_awaddr,  input  wire s_axi_awvalid, output wire s_axi_awready,
    input  wire [31:0] s_axi_wdata,   input  wire [3:0] s_axi_wstrb,
    input  wire        s_axi_wvalid,  output wire s_axi_wready,
    output wire [1:0]  s_axi_bresp,   output wire s_axi_bvalid,  input  wire s_axi_bready,
    input  wire [5:0]  s_axi_araddr,  input  wire s_axi_arvalid, output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,   output wire [1:0] s_axi_rresp,
    output wire        s_axi_rvalid,  input  wire s_axi_rready,

    // HDMI video (from v_vid_in_axi4s)
    input  wire [23:0] s_axis_hdmi_tdata,  input  wire s_axis_hdmi_tvalid, output wire s_axis_hdmi_tready,
    input  wire        s_axis_hdmi_tuser,  input  wire s_axis_hdmi_tlast,
    input  wire        hdmi_locked,                // asynchronous, synchronised here

    // test pattern video (from v_tpg)
    input  wire [23:0] s_axis_tpg_tdata,   input  wire s_axis_tpg_tvalid,  output wire s_axis_tpg_tready,
    input  wire        s_axis_tpg_tuser,   input  wire s_axis_tpg_tlast,

    // normalised output
    output wire [23:0] m_axis_tdata,     output wire m_axis_tvalid,    input  wire m_axis_tready,
    output wire        m_axis_tuser,     output wire m_axis_tlast,

    // status for LEDs
    output wire        st_hdmi_good,
    output wire        st_fallback,
    output wire        st_src_tpg
);
    localparam [31:0] ID = 32'h4953_4C01;

    // ------------------------------------------------------------ registers
    reg         r_src, r_fb_en;
    reg  [31:0] r_timeout, r_return, r_period;
    reg         abort_sticky;

    wire        wr_en;  wire [5:0] wr_addr, rd_addr;  wire [31:0] wr_data;  wire [3:0] wr_strb;
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

    // ------------------------------------------------------------ HDMI lock sync
    reg [2:0] lock_sr;
    always @(posedge aclk) lock_sr <= {lock_sr[1:0], hdmi_locked};
    wire lock_s = lock_sr[2];

    // ------------------------------------------------------------ HDMI measurement
    reg [15:0] px_cnt, ln_cnt, last_w, meas_w, meas_h;
    reg        bad_line, dims_ok;
    reg [31:0] since_sof, hdmi_frames, out_frames;
    wire       hdmi_xfer = s_axis_hdmi_tvalid && s_axis_hdmi_tready;
    wire       frame_alive = (since_sof < WATCHDOG);
    wire       hdmi_good   = lock_s && dims_ok && frame_alive;

    always @(posedge aclk) begin
        if (!aresetn) begin
            px_cnt <= 0; ln_cnt <= 0; last_w <= 0; meas_w <= 0; meas_h <= 0;
            bad_line <= 0; dims_ok <= 0; since_sof <= 32'hFFFF_FFFF; hdmi_frames <= 0;
        end else begin
            if (since_sof != 32'hFFFF_FFFF) since_sof <= since_sof + 1;
            if (hdmi_xfer) begin
                if (s_axis_hdmi_tuser) begin
                    meas_w   <= last_w;
                    meas_h   <= ln_cnt;
                    dims_ok  <= (ln_cnt == H) && !bad_line;
                    bad_line <= 1'b0;
                    ln_cnt   <= 0;
                    px_cnt   <= (s_axis_hdmi_tlast ? 16'd0 : 16'd1);
                    since_sof <= 0;
                    hdmi_frames <= hdmi_frames + 1;
                    if (s_axis_hdmi_tlast) begin last_w <= 16'd1; ln_cnt <= 16'd1; if (W != 1) bad_line <= 1'b1; end
                end else if (s_axis_hdmi_tlast) begin
                    last_w <= px_cnt + 1;
                    if (px_cnt + 1 != W) bad_line <= 1'b1;
                    px_cnt <= 0;
                    ln_cnt <= ln_cnt + 1;
                end else begin
                    px_cnt <= px_cnt + 1;
                end
            end
        end
    end

    // ------------------------------------------------------------ fallback logic
    reg [31:0] bad_cnt, good_cnt;
    reg        fallback;
    always @(posedge aclk) begin
        if (!aresetn) begin
            bad_cnt <= 0; good_cnt <= 0; fallback <= 1'b0;
        end else if (!r_fb_en || r_src) begin
            bad_cnt <= 0; good_cnt <= 0; fallback <= 1'b0;
        end else if (hdmi_good) begin
            bad_cnt <= 0;
            if (fallback) begin
                if (good_cnt >= r_return) begin fallback <= 1'b0; good_cnt <= 0; end
                else good_cnt <= good_cnt + 1;
            end
        end else begin
            good_cnt <= 0;
            if (!fallback) begin
                if (bad_cnt >= r_timeout) begin fallback <= 1'b1; bad_cnt <= 0; end
                else bad_cnt <= bad_cnt + 1;
            end
        end
    end

    wire want_tpg = r_src | fallback;

    // ------------------------------------------------------------ normaliser / mux
    localparam [1:0] S_WAIT = 2'd0, S_FWD = 2'd1, S_PAD = 2'd2;
    reg [1:0]  state;
    reg        cur_tpg;
    reg [10:0] x;
    reg [9:0]  y;
    reg [31:0] pace_cnt, stall_cnt;

    // Digilent dvi2rgb order is {R,B,G}; the pipeline order is {R,G,B}
    wire [23:0] hdmi_px  = {s_axis_hdmi_tdata[23:16], s_axis_hdmi_tdata[7:0], s_axis_hdmi_tdata[15:8]};
    wire [23:0] s_tdata  = cur_tpg ? s_axis_tpg_tdata  : hdmi_px;
    wire        s_tvalid = cur_tpg ? s_axis_tpg_tvalid : s_axis_hdmi_tvalid;
    wire        s_tuser  = cur_tpg ? s_axis_tpg_tuser  : s_axis_hdmi_tuser;
    wire        s_tlast  = cur_tpg ? s_axis_tpg_tlast  : s_axis_hdmi_tlast;

    wire last_x   = (x == W - 1);
    wire last_y   = (y == H - 1);
    wire pace_ok  = (pace_cnt >= r_period);
    wire go_ok    = cur_tpg ? pace_ok : hdmi_good;
    wire sof_go   = (state == S_WAIT) && s_tvalid && s_tuser && go_ok;

    // source ready
    // WAIT: drain everything except a start-of-frame beat we intend to forward (or, for the
    // test pattern, one that is waiting for the pacer).  A bad HDMI SOF is simply dropped.
    wire hold_sof = s_tvalid && s_tuser && (cur_tpg || hdmi_good);
    wire s_tready = (state == S_FWD)  ? m_axis_tready :
                    (state == S_PAD)  ? 1'b0     :
                                        !hold_sof;
    assign s_axis_hdmi_tready = cur_tpg ? 1'b1 : s_tready;
    assign s_axis_tpg_tready  = cur_tpg ? s_tready : 1'b0;

    assign m_axis_tvalid = (state == S_FWD) ? s_tvalid : (state == S_PAD);
    assign m_axis_tdata  = (state == S_FWD) ? s_tdata  : 24'd0;
    assign m_axis_tlast  = last_x;
    assign m_axis_tuser  = (x == 0) && (y == 0);

    wire out_xfer = m_axis_tvalid && m_axis_tready;
    wire bad_beat = (s_tlast != last_x) || (s_tuser && !((x == 0) && (y == 0)));

    always @(posedge aclk) begin
        if (!aresetn) begin
            state <= S_WAIT; cur_tpg <= 1'b0; x <= 0; y <= 0;
            pace_cnt <= 32'hFFFF_FFFF; stall_cnt <= 0; out_frames <= 0; abort_sticky <= 1'b0;
        end else begin
            if (pace_cnt != 32'hFFFF_FFFF) pace_cnt <= pace_cnt + 1;
            if (wr_en && wr_addr[5:2] == 4'h2 && wr_strb[0] && wr_data[4]) abort_sticky <= 1'b0;

            case (state)
            S_WAIT: begin
                x <= 0; y <= 0; stall_cnt <= 0;
                if (cur_tpg != want_tpg) begin
                    cur_tpg <= want_tpg;                       // frame boundary: safe to switch
                end else if (sof_go) begin
                    state <= S_FWD;
                    if (cur_tpg) pace_cnt <= 0;
                end
            end
            S_FWD: begin
                if (out_xfer) begin
                    stall_cnt <= 0;
                    // the source's own end-of-line must agree with our line counter
                    if (bad_beat) begin
                        abort_sticky <= 1'b1;
                        if (last_x) begin
                            // long line: nothing to pad on this line; finish via pad
                            if (last_y) begin state <= S_WAIT; out_frames <= out_frames + 1; end
                            else begin state <= S_PAD; x <= 0; y <= y + 1; end
                        end else begin
                            state <= S_PAD; x <= x + 1;
                        end
                    end else if (last_x) begin
                        x <= 0;
                        if (last_y) begin state <= S_WAIT; y <= 0; out_frames <= out_frames + 1; end
                        else y <= y + 1;
                    end else begin
                        x <= x + 1;
                    end
                end else if (!s_tvalid && m_axis_tready) begin
                    if (stall_cnt >= STALL_LIMIT) begin abort_sticky <= 1'b1; state <= S_PAD; end
                    else stall_cnt <= stall_cnt + 1;
                end
            end
            S_PAD: begin
                if (out_xfer) begin
                    if (last_x) begin
                        x <= 0;
                        if (last_y) begin state <= S_WAIT; y <= 0; out_frames <= out_frames + 1; end
                        else y <= y + 1;
                    end else x <= x + 1;
                end
            end
            default: state <= S_WAIT;
            endcase

            // control register writes
            if (wr_en) begin
                case (wr_addr[5:2])
                    4'h1: if (wr_strb[0]) begin r_src <= wr_data[0]; r_fb_en <= wr_data[1]; end
                    4'h3: if (&wr_strb) r_timeout <= wr_data;
                    4'h4: if (&wr_strb) r_return  <= wr_data;
                    4'h8: if (&wr_strb && wr_data != 0) r_period <= wr_data;
                    default: ;
                endcase
            end
        end
        if (!aresetn) begin
            r_src <= 1'b0; r_fb_en <= 1'b1;
            r_timeout <= 5 * CLK_HZ; r_return <= CLK_HZ; r_period <= CLK_HZ / 60;
        end
    end

    always @* begin
        case (rd_addr[5:2])
            4'h0: rd_data = ID;
            4'h1: rd_data = {30'd0, r_fb_en, r_src};
            4'h2: rd_data = {27'd0, abort_sticky, cur_tpg, fallback, hdmi_good, lock_s};
            4'h3: rd_data = r_timeout;
            4'h4: rd_data = r_return;
            4'h5: rd_data = {meas_h, meas_w};
            4'h6: rd_data = hdmi_frames;
            4'h7: rd_data = out_frames;
            4'h8: rd_data = r_period;
            default: rd_data = 32'd0;
        endcase
    end

    assign st_hdmi_good = hdmi_good;
    assign st_fallback  = fallback;
    assign st_src_tpg   = cur_tpg;
endmodule
