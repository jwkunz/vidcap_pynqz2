// Self-checking testbench for vc_input_sel (scaled down: 8x4 frames, short timers).
`timescale 1ns/1ps
module tb_vc_input_sel;
    localparam W = 8, H = 4;
    reg aclk = 0, aresetn = 0;
    always #5 aclk = ~aclk;

    // ---------------- AXI-Lite master
    reg  [5:0] awaddr = 0, araddr = 0;  reg awvalid = 0, wvalid = 0, arvalid = 0;
    reg  [31:0] wdata = 0;
    wire awready, wready, bvalid, arready, rvalid;
    wire [1:0] bresp, rresp;  wire [31:0] rdata;

    // ---------------- streams
    reg  [23:0] hdmi_tdata = 0;  reg hdmi_tvalid = 0, hdmi_tuser = 0, hdmi_tlast = 0;  wire hdmi_tready;
    reg  hdmi_locked = 0;
    reg  [23:0] tpg_tdata = 0;   reg tpg_tvalid = 0,  tpg_tuser = 0,  tpg_tlast = 0;   wire tpg_tready;
    wire [23:0] m_tdata;  wire m_tvalid, m_tuser, m_tlast;
    reg  m_tready = 1;
    wire st_good, st_fb, st_tpg;

    vc_input_sel #(.W(W), .H(H), .CLK_HZ(1000), .WATCHDOG(300), .STALL_LIMIT(200)) dut (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(1'b1),
        .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(1'b1),
        .s_axis_hdmi_tdata(hdmi_tdata), .s_axis_hdmi_tvalid(hdmi_tvalid), .s_axis_hdmi_tready(hdmi_tready),
        .s_axis_hdmi_tuser(hdmi_tuser), .s_axis_hdmi_tlast(hdmi_tlast), .hdmi_locked(hdmi_locked),
        .s_axis_tpg_tdata(tpg_tdata), .s_axis_tpg_tvalid(tpg_tvalid), .s_axis_tpg_tready(tpg_tready),
        .s_axis_tpg_tuser(tpg_tuser), .s_axis_tpg_tlast(tpg_tlast),
        .m_axis_tdata(m_tdata), .m_axis_tvalid(m_tvalid), .m_axis_tready(m_tready),
        .m_axis_tuser(m_tuser), .m_axis_tlast(m_tlast),
        .st_hdmi_good(st_good), .st_fallback(st_fb), .st_src_tpg(st_tpg));

    integer fails = 0;
    task check(input c, input [511:0] msg);
        begin if (!c) begin fails = fails + 1; $display("FAIL: %0s (t=%0t)", msg, $time); end
              else $display("ok   %0s", msg); end
    endtask
    task wait_cycles(input integer n); begin repeat (n) @(posedge aclk); end endtask

    task axi_write(input [5:0] a, input [31:0] d);
        begin
            @(posedge aclk); awaddr <= a; wdata <= d; awvalid <= 1; wvalid <= 1;
            wait (awready && wready) @(posedge aclk);
            awvalid <= 0; wvalid <= 0;
            wait (bvalid) @(posedge aclk);
        end
    endtask
    reg [31:0] rd;
    task axi_read(input [5:0] a);
        begin
            @(posedge aclk); araddr <= a; arvalid <= 1;
            wait (arready) @(posedge aclk);
            arvalid <= 0;
            wait (rvalid);
            rd = rdata;
            @(posedge aclk);
        end
    endtask

    // ---------------- HDMI source model
    reg        hdmi_run = 0;
    integer    hdmi_gap = 10, hdmi_short_line = -1, hdmi_long_line = -1, hdmi_hang_at = -1;
    task hbeat(input [23:0] d, input u, input l);
        begin
            hdmi_tdata <= d; hdmi_tuser <= u; hdmi_tlast <= l; hdmi_tvalid <= 1'b1;
            @(posedge aclk);
            while (!hdmi_tready) @(posedge aclk);
            hdmi_tvalid <= 1'b0;
        end
    endtask
    initial begin : hdmi_gen
        integer lx, ly, wl, bi;
        forever begin
            @(posedge aclk);
            if (hdmi_run) begin
                bi = 0;
                for (ly = 0; ly < H; ly = ly + 1) begin
                    wl = (ly == hdmi_short_line) ? W - 3 : (ly == hdmi_long_line) ? W + 2 : W;
                    for (lx = 0; lx < wl; lx = lx + 1) begin
                        if (bi == hdmi_hang_at) begin wait (hdmi_hang_at < 0); end
                        hbeat({8'hB0, lx[7:0], ly[7:0]}, (lx == 0 && ly == 0), (lx == wl - 1));
                        bi = bi + 1;
                    end
                end
                hdmi_short_line = -1; hdmi_long_line = -1;
                repeat (hdmi_gap) @(posedge aclk);
            end
        end
    end

    // ---------------- test pattern model (back to back, relies on the pacer)
    task tbeat(input [23:0] d, input u, input l);
        begin
            tpg_tdata <= d; tpg_tuser <= u; tpg_tlast <= l; tpg_tvalid <= 1'b1;
            @(posedge aclk);
            while (!tpg_tready) @(posedge aclk);
            tpg_tvalid <= 1'b0;
        end
    endtask
    initial begin : tpg_gen
        integer lx, ly;
        forever begin
            @(posedge aclk);
            for (ly = 0; ly < H; ly = ly + 1)
                for (lx = 0; lx < W; lx = lx + 1)
                    tbeat({8'hA0, ly[7:0], lx[7:0]}, (lx == 0 && ly == 0), (lx == W - 1));
        end
    end

    // ---------------- output monitor
    integer mx = 0, my = 0, nframes = 0, struct_err = 0, mixed_err = 0, pos_err = 0;
    integer cntA = 0, cntB = 0, cntP = 0, padded = 0, fpad = 0;
    reg [7:0] ftag = 0;
    reg [7:0] last_tag = 0;
    time last_sof = 0, min_sof_gap = 64'hFFFF_FFFF;
    always @(posedge aclk) if (aresetn && m_tvalid && m_tready) begin
        if (m_tuser !== (mx == 0 && my == 0)) struct_err = struct_err + 1;
        if (m_tlast !== (mx == W - 1))        struct_err = struct_err + 1;
        if (m_tuser) begin
            if (last_sof != 0 && ($time - last_sof) < min_sof_gap && last_tag == 8'hA0 && m_tdata[23:16] == 8'hA0)
                min_sof_gap = $time - last_sof;
            last_sof = $time;
        end
        if (m_tdata == 24'd0) fpad = fpad + 1;
        else begin
            if (ftag != 0 && ftag != m_tdata[23:16]) mixed_err = mixed_err + 1;
            if (ftag == 0) ftag = m_tdata[23:16];
            if (m_tdata[15:8] != my || m_tdata[7:0] != mx) pos_err = pos_err + 1;
        end
        if (mx == W - 1) begin
            mx = 0;
            if (my == H - 1) begin
                my = 0; nframes = nframes + 1; last_tag = ftag;
                if (ftag == 8'hA0) cntA = cntA + 1;
                else if (ftag == 8'hB0) cntB = cntB + 1;
                else cntP = cntP + 1;
                if (fpad != 0) padded = padded + 1;
                ftag = 0; fpad = 0;
            end else my = my + 1;
        end else mx = mx + 1;
    end

    task clear_counts; begin cntA = 0; cntB = 0; cntP = 0; padded = 0; end endtask
    task status; begin axi_read(6'h08); end endtask   // result in rd

    integer snapA, snapB, snapN;
    initial begin
        repeat (5) @(posedge aclk); aresetn <= 1; wait_cycles(5);

        // ---- registers / defaults
        axi_read(6'h00); check(rd === 32'h4953_4C01, "ID");
        axi_read(6'h04); check(rd === 32'h2, "CTRL default: HDMI source, fallback enabled");
        axi_read(6'h0C); check(rd === 5000, "TIMEOUT default = 5 s");
        axi_read(6'h10); check(rd === 1000, "RETURN default = 1 s");
        axi_write(6'h0C, 2000); axi_write(6'h10, 1000); axi_write(6'h20, 100);
        axi_read(6'h0C); check(rd === 2000, "TIMEOUT write");

        // ---- T1: test pattern, paced
        axi_write(6'h04, 32'h3); wait_cycles(10); clear_counts;
        wait_cycles(3000);
        check(cntA >= 25 && cntB == 0 && cntP == 0, "T1 only test-pattern frames");
        check(min_sof_gap >= 100 * 10, "T1 test pattern paced to >= 100 clocks per frame");
        check(min_sof_gap < 105 * 10, "T1 pacing is not slower than configured");
        status; check(rd[3] === 1'b1 && rd[1] === 1'b0, "T1 status: active src TPG, HDMI not good");

        // ---- T2: HDMI good, forwarded
        hdmi_locked = 1; hdmi_run = 1; axi_write(6'h04, 32'h2);
        wait_cycles(1500); clear_counts; wait_cycles(1500);
        check(cntB >= 20 && cntA == 0 && cntP == 0, "T2 only HDMI frames");
        check(last_tag == 8'hB0, "T2 last frame is HDMI");
        status; check(rd[1:0] === 2'b11 && rd[2] === 1'b0 && rd[3] === 1'b0, "T2 status: locked, good, no fallback");
        axi_read(6'h14); check(rd === {16'd4, 16'd8}, "T2 measured 8x4");

        // ---- T3: HDMI stops -> fallback after TIMEOUT
        hdmi_run = 0; wait_cycles(1200); status;
        check(rd[2] === 1'b0, "T3 not in fallback before the timeout");
        wait_cycles(2500); status;
        check(rd[2] === 1'b1 && rd[3] === 1'b1, "T3 fallback active, TPG selected");
        clear_counts; wait_cycles(1500);
        check(cntA >= 10 && cntB == 0, "T3 output keeps running on the test pattern");

        // ---- T4: HDMI returns -> back after RETURN
        hdmi_run = 1; wait_cycles(600); status;
        check(rd[2] === 1'b1, "T4 still in fallback before RETURN elapsed");
        wait_cycles(2500); status;
        check(rd[2] === 1'b0 && rd[3] === 1'b0, "T4 back on HDMI");
        clear_counts; wait_cycles(800);
        check(cntB >= 8 && cntA == 0, "T4 HDMI frames again");

        // ---- T5: malformed HDMI frames are completed with black
        clear_counts; hdmi_short_line = 2; wait_cycles(400);
        check(padded >= 1 && struct_err == 0, "T5 short line: frame padded, structure intact");
        status; check(rd[4] === 1'b1, "T5 abort flag set"); axi_write(6'h08, 32'h10);
        status; check(rd[4] === 1'b0, "T5 abort flag cleared");
        wait_cycles(600);
        hdmi_long_line = 1; wait_cycles(600);
        check(struct_err == 0, "T5 long line: structure intact");
        wait_cycles(600); clear_counts; wait_cycles(800);
        check(cntB >= 8 && padded == 0, "T5 clean HDMI frames resume");

        // ---- T6: source hangs mid-frame -> padded
        clear_counts; hdmi_hang_at = 10; wait_cycles(900);
        check(padded >= 1 && struct_err == 0, "T6 hung source: frame completed with padding");
        hdmi_hang_at = -1; wait_cycles(1200);

        // ---- T7: software switch at a frame boundary
        clear_counts; axi_write(6'h04, 32'h3); wait_cycles(800);
        check(last_tag == 8'hA0 && mixed_err == 0 && struct_err == 0, "T7 switched to TPG cleanly");
        axi_write(6'h04, 32'h2); wait_cycles(1800);
        check(last_tag == 8'hB0 && mixed_err == 0, "T7 switched back to HDMI cleanly");

        // ---- T8: lost lock counts as bad input
        hdmi_locked = 0; wait_cycles(3000); status;
        check(rd[0] === 1'b0 && rd[1] === 1'b0 && rd[2] === 1'b1, "T8 lock lost -> fallback");
        hdmi_locked = 1; wait_cycles(3000); status;
        check(rd[2] === 1'b0, "T8 lock back -> HDMI again");

        // ---- T9: fallback disabled
        axi_write(6'h04, 32'h0); hdmi_run = 0; wait_cycles(1000); clear_counts; wait_cycles(4000); status;
        check(rd[2] === 1'b0 && rd[3] === 1'b0 && cntA == 0, "T9 fallback disabled: stays on HDMI, no test pattern");

        // ---- whole-run invariants
        check(struct_err == 0, "no structural errors in any output frame");
        check(mixed_err == 0,  "no frame ever mixed two sources");
        check(pos_err == 0,    "every pixel was at the right position");

        if (fails == 0) $display("TEST PASSED"); else $display("TEST FAILED (%0d)", fails);
        $finish;
    end
    initial begin #20_000_000; $display("TEST FAILED (timeout)"); $finish; end
endmodule
