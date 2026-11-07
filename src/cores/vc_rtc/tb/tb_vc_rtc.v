// Self-checking testbench for vc_rtc. CLK_HZ is tiny so one "second" is a few clocks.
`timescale 1ns/1ps
module tb_vc_rtc;
    reg aclk = 0, aresetn = 0;
    always #5 aclk = ~aclk;

    reg  [5:0] awaddr = 0, araddr = 0;  reg awvalid = 0, wvalid = 0, bready = 1, arvalid = 0, rready = 1;
    reg  [31:0] wdata = 0;
    wire awready, wready, bvalid, arready, rvalid;
    wire [1:0] bresp, rresp;
    wire [31:0] rdata;
    wire [55:0] digits;
    wire valid;

    vc_rtc #(.CLK_HZ(8)) dut (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .time_digits(digits), .time_valid(valid));

    integer fails = 0;

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

    // set clock via shadow regs + LOAD
    task set_time(input integer c, input integer y, input integer mo, input integer d,
                  input integer h, input integer mi, input integer s);
        begin
            axi_write(6'h0C, (d << 24) | (mo << 16) | (c << 8) | y);
            axi_write(6'h10, (s << 16) | (mi << 8) | h);
            axi_write(6'h04, 32'h1);
        end
    endtask

    // latch + read back, compare against expected fields
    task expect_time(input integer c, input integer y, input integer mo, input integer d,
                     input integer h, input integer mi, input integer s, input [255:0] tag);
        reg [31:0] dd, tt;
        begin
            axi_write(6'h04, 32'h2);
            axi_read(6'h0C); dd = rd;
            axi_read(6'h10); tt = rd;
            if (dd[28:24] !== d || dd[19:16] !== mo || dd[14:8] !== c || dd[6:0] !== y ||
                tt[4:0] !== h || tt[13:8] !== mi || tt[21:16] !== s) begin
                fails = fails + 1;
                $display("FAIL %0s: got %0d%0d-%0d-%0d %0d:%0d:%0d expected %0d%0d-%0d-%0d %0d:%0d:%0d", tag,
                    dd[14:8], dd[6:0], dd[19:16], dd[28:24], tt[4:0], tt[13:8], tt[21:16], c, y, mo, d, h, mi, s);
            end else $display("ok   %0s", tag);
        end
    endtask

    // wait for N seconds (8 clocks each) of the live clock
    task wait_secs(input integer n);
        begin repeat (n * 8) @(posedge aclk); end
    endtask

    initial begin
        repeat (5) @(posedge aclk); aresetn <= 1; repeat (3) @(posedge aclk);

        // 1. power-up value
        if (digits !== 56'h20_26_01_01_00_00_00 && digits[55:8] !== 48'h2026_0101_0000) begin
            fails = fails + 1; $display("FAIL powerup digits %h", digits);
        end else $display("ok   powerup digits %h valid=%b", digits, valid);
        if (valid !== 1'b0) begin fails = fails + 1; $display("FAIL valid should be 0 at reset"); end
        axi_read(6'h00); if (rd !== 32'h5254_4301) begin fails = fails + 1; $display("FAIL id %h", rd); end

        // 2. leap day 2024-02-28 23:59:59 -> 02-29 00:00:00 -> 03-01
        set_time(20, 24, 2, 28, 23, 59, 59);
        axi_read(6'h08); if (rd[0] !== 1'b1) begin fails = fails + 1; $display("FAIL valid not set"); end
        wait_secs(1); expect_time(20, 24, 2, 29, 0, 0, 0, "2024 leap day exists");
        wait_secs(86400 * 0 + 1);   // only one more tick; verify 03-01 via a fresh load below

        set_time(20, 24, 2, 29, 23, 59, 59);
        wait_secs(1); expect_time(20, 24, 3, 1, 0, 0, 0, "2024-02-29 -> 03-01");

        // 3. non-leap 2025
        set_time(20, 25, 2, 28, 23, 59, 59);
        wait_secs(1); expect_time(20, 25, 3, 1, 0, 0, 0, "2025-02-28 -> 03-01");

        // 4. century rules: 1900 not leap, 2000 leap
        set_time(19, 0, 2, 28, 23, 59, 59);
        wait_secs(1); expect_time(19, 0, 3, 1, 0, 0, 0, "1900 not leap");
        set_time(20, 0, 2, 28, 23, 59, 59);
        wait_secs(1); expect_time(20, 0, 2, 29, 0, 0, 0, "2000 leap");

        // 5. year / month / hour / minute rollovers
        set_time(20, 26, 12, 31, 23, 59, 59);
        wait_secs(1); expect_time(20, 27, 1, 1, 0, 0, 0, "2026-12-31 -> 2027-01-01");
        set_time(20, 26, 4, 30, 23, 59, 59);
        wait_secs(1); expect_time(20, 26, 5, 1, 0, 0, 0, "30-day month");
        set_time(20, 26, 6, 15, 12, 59, 59);
        wait_secs(1); expect_time(20, 26, 6, 15, 13, 0, 0, "hour rollover");
        set_time(20, 99, 12, 31, 23, 59, 59);
        wait_secs(1); expect_time(21, 0, 1, 1, 0, 0, 0, "2099 -> 2100");

        // 6. invalid loads are rejected and flagged
        set_time(20, 26, 2, 30, 0, 0, 0);          // Feb 30
        axi_read(6'h08); if (rd[1] !== 1'b1) begin fails = fails + 1; $display("FAIL err not set for Feb 30"); end
        else $display("ok   Feb 30 rejected");
        expect_time(21, 0, 1, 1, 0, 0, 3, "clock unchanged after bad load");   // clock kept running
        axi_write(6'h04, 32'h4);
        axi_read(6'h08); if (rd[1] !== 1'b0) begin fails = fails + 1; $display("FAIL err not cleared"); end

        // 7. digits output
        set_time(20, 26, 11, 7, 9, 41, 3);
        repeat (2) @(posedge aclk);
        if (digits !== 56'h20_26_11_07_09_41_03) begin
            fails = fails + 1; $display("FAIL digits %h", digits);
        end else $display("ok   digits 20261107094103");

        // 8. CPS trim: 16 clocks per second halves the tick rate
        axi_write(6'h14, 32'd16);
        set_time(20, 26, 1, 1, 0, 0, 0);
        repeat (16 * 3 + 4) @(posedge aclk);
        expect_time(20, 26, 1, 1, 0, 0, 3, "CPS trim");

        if (fails == 0) $display("TEST PASSED"); else $display("TEST FAILED (%0d)", fails);
        $finish;
    end

    initial begin #50_000_000; $display("TEST FAILED (timeout)"); $finish; end
endmodule
