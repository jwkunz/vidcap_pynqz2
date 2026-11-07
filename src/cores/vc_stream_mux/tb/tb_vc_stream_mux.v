// Self-checking testbench for vc_stream_mux (8x4 frames).
`timescale 1ns/1ps
module tb_vc_stream_mux;
    localparam W = 8, H = 4;
    reg aclk = 0, aresetn = 0; always #5 aclk = ~aclk;

    reg  [3:0] awaddr = 0, araddr = 0;  reg awvalid = 0, wvalid = 0, arvalid = 0;  reg [31:0] wdata = 0;
    wire awready, wready, bvalid, arready, rvalid;  wire [1:0] bresp, rresp;  wire [31:0] rdata;

    reg  [23:0] ld = 0, vd = 0;  reg lv = 0, vv = 0, lu = 0, vu = 0, ll = 0, vl = 0;
    wire lr, vr;
    wire [23:0] md; wire mv, mu, ml;  reg mr = 1;

    vc_stream_mux #(.H(H)) dut (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(1'b1),
        .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(1'b1),
        .s_axis_live_tdata(ld), .s_axis_live_tvalid(lv), .s_axis_live_tready(lr), .s_axis_live_tuser(lu), .s_axis_live_tlast(ll),
        .s_axis_vmem_tdata(vd), .s_axis_vmem_tvalid(vv), .s_axis_vmem_tready(vr), .s_axis_vmem_tuser(vu), .s_axis_vmem_tlast(vl),
        .m_axis_tdata(md), .m_axis_tvalid(mv), .m_axis_tready(mr), .m_axis_tuser(mu), .m_axis_tlast(ml));

    integer fails = 0;
    task check(input c, input [511:0] msg);
        begin if (!c) begin fails = fails + 1; $display("FAIL: %0s (t=%0t)", msg, $time); end else $display("ok   %0s", msg); end
    endtask
    task axi_write(input [3:0] a, input [31:0] d);
        begin
            @(posedge aclk); awaddr <= a; wdata <= d; awvalid <= 1; wvalid <= 1;
            wait (awready && wready) @(posedge aclk);
            awvalid <= 0; wvalid <= 0; wait (bvalid) @(posedge aclk);
        end
    endtask
    reg [31:0] rd;
    task axi_read(input [3:0] a);
        begin
            @(posedge aclk); araddr <= a; arvalid <= 1; wait (arready) @(posedge aclk);
            arvalid <= 0; wait (rvalid); rd = rdata; @(posedge aclk);
        end
    endtask

    // continuous frame generators: live tag 0xA0, vmem tag 0xB0, pixel = {tag, y, x}
    task gen_beat_l(input [23:0] d, input u, input l);
        begin ld <= d; lu <= u; ll <= l; lv <= 1; @(posedge aclk); while (!lr) @(posedge aclk); lv <= 0; end
    endtask
    task gen_beat_v(input [23:0] d, input u, input l);
        begin vd <= d; vu <= u; vl <= l; vv <= 1; @(posedge aclk); while (!vr) @(posedge aclk); vv <= 0; end
    endtask
    initial begin : live_gen
        integer x, y; forever begin @(posedge aclk);
            for (y = 0; y < H; y = y + 1) for (x = 0; x < W; x = x + 1)
                gen_beat_l({8'hA0, y[7:0], x[7:0]}, (x == 0 && y == 0), (x == W - 1));
        end
    end
    initial begin : vmem_gen
        integer x, y; forever begin @(posedge aclk);
            for (y = 0; y < H; y = y + 1) for (x = 0; x < W; x = x + 1)
                gen_beat_v({8'hB0, y[7:0], x[7:0]}, (x == 0 && y == 0), (x == W - 1));
        end
    end

    // monitor
    integer mx = 0, my = 0, frames = 0, struct_err = 0, mixed_err = 0, pos_err = 0, cntA = 0, cntB = 0;
    reg [7:0] ftag = 0, last_tag = 0;
    always @(posedge aclk) if (aresetn && mv && mr) begin
        if (mu !== (mx == 0 && my == 0)) struct_err = struct_err + 1;
        if (ml !== (mx == W - 1)) struct_err = struct_err + 1;
        if (ftag != 0 && ftag != md[23:16]) mixed_err = mixed_err + 1;
        ftag = md[23:16];
        if (md[15:8] != my || md[7:0] != mx) pos_err = pos_err + 1;
        if (mx == W - 1) begin
            mx = 0;
            if (my == H - 1) begin
                my = 0; frames = frames + 1; last_tag = ftag;
                if (ftag == 8'hA0) cntA = cntA + 1; else cntB = cntB + 1;
                ftag = 0;
            end else my = my + 1;
        end else mx = mx + 1;
    end

    initial begin
        repeat (5) @(posedge aclk); aresetn <= 1; repeat (200) @(posedge aclk);
        axi_read(4'h0); check(rd === 32'h4D55_5801, "ID");
        check(cntA > 3 && cntB == 0, "default: only live frames");
        axi_read(4'h8); check(rd[1:0] === 2'b00, "status: live, nothing pending");

        axi_write(4'h4, 1); repeat (300) @(posedge aclk);
        axi_read(4'h8); check(rd[0] === 1'b1 && rd[1] === 1'b0, "switched to video memory");
        check(last_tag == 8'hB0 && cntB > 3, "video memory frames flow");

        // backpressure must not corrupt anything
        repeat (400) begin @(posedge aclk); mr <= ($random & 3) != 0; end
        mr <= 1; repeat (100) @(posedge aclk);

        // switch back and forth quickly
        axi_write(4'h4, 0); repeat (300) @(posedge aclk);
        check(last_tag == 8'hA0, "back to live");
        axi_write(4'h4, 1); axi_write(4'h4, 0); axi_write(4'h4, 1); repeat (300) @(posedge aclk);
        check(last_tag == 8'hB0, "rapid toggling ends on the last request");
        axi_read(4'hC); check(rd == frames, "frame counter matches monitor");

        check(struct_err == 0, "no framing errors");
        check(mixed_err == 0, "no frame ever mixed live and video memory");
        check(pos_err == 0, "every pixel at the right position");
        if (fails == 0) $display("TEST PASSED"); else $display("TEST FAILED (%0d)", fails);
        $finish;
    end
    initial begin #5_000_000; $display("TEST FAILED (timeout)"); $finish; end
endmodule
