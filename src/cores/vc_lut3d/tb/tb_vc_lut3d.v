// tb_vc_lut3d.v - Self-checking testbench for vc_lut3d.
// Needs the vectors from gen_vectors.py in ../lut_vectors (relative to the xsim run directory); `make test-rtl` makes them.
`timescale 1ns/1ps
module tb_vc_lut3d;
    localparam N = 640, LINE = 128;
    reg clk = 0, aresetn = 0;
    always #5 clk = ~clk;

    reg  [15:0] awaddr = 0, araddr = 0;  reg awvalid = 0, arvalid = 0, wvalid = 0, bready = 1, rready = 1;
    reg  [31:0] wdata = 0;
    wire awready, wready, bvalid, arready, rvalid;  wire [1:0] bresp, rresp;  wire [31:0] rdata;

    reg  [23:0] s_tdata = 0;  reg s_tvalid = 0, s_tuser = 0, s_tlast = 0;  wire s_tready;
    wire [23:0] m_tdata;      wire m_tvalid, m_tuser, m_tlast;  reg m_tready = 0;

    vc_lut3d dut (
        .aclk(clk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awvalid(awvalid), .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid), .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .s_axis_tdata(s_tdata), .s_axis_tvalid(s_tvalid), .s_axis_tready(s_tready),
        .s_axis_tuser(s_tuser), .s_axis_tlast(s_tlast),
        .m_axis_tdata(m_tdata), .m_axis_tvalid(m_tvalid), .m_axis_tready(m_tready),
        .m_axis_tuser(m_tuser), .m_axis_tlast(m_tlast));

    integer errors = 0;
    task check(input [511:0] name, input ok);
        begin
            if (ok) $display("  [PASS] %0s", name);
            else begin $display("  [FAIL] %0s", name); errors = errors + 1; end
        end
    endtask

    // ---- AXI-Lite master
    task axi_write(input [15:0] a, input [31:0] d);
        reg a_done, w_done;
        begin
            @(negedge clk); awaddr = a; awvalid = 1; wdata = d; wvalid = 1; a_done = 0; w_done = 0;
            while (!(a_done && w_done)) begin
                @(posedge clk);
                if (awready) a_done = 1;
                if (wready)  w_done = 1;
                #1; if (a_done) awvalid = 0; if (w_done) wvalid = 0;
            end
            repeat (3) @(posedge clk);
        end
    endtask

    task axi_read(input [15:0] a, output [31:0] d);
        begin
            @(negedge clk); araddr = a; arvalid = 1;
            @(posedge clk); while (!arready) @(posedge clk);
            d = rdata;                                  // rvalid is high in the same cycle as the arready pulse
            #1 arvalid = 0;
            repeat (3) @(posedge clk);
        end
    endtask

    // ---- vectors
    reg [23:0] px [0:N-1];
    reg [23:0] e_ident [0:N-1], e_full [0:N-1], e_half [0:N-1], expv [0:N-1];
    reg [31:0] banks [0:8191];
    integer i;
    initial begin
        $readmemh("../lut_vectors/px.hex", px);
        $readmemh("../lut_vectors/exp_ident.hex", e_ident);
        $readmemh("../lut_vectors/exp_full.hex", e_full);
        $readmemh("../lut_vectors/exp_half.hex", e_half);
        $readmemh("../lut_vectors/banks_rand.hex", banks);
    end

    // ---- sink: random back-pressure, compares in order
    integer got = 0, bad = 0;
    always @(posedge clk) begin
        if (m_tvalid && m_tready) begin
            if (got < N) begin
                if (m_tdata !== expv[got]) begin
                    bad = bad + 1;
                    if (bad < 6) $display("    pixel %0d: got %06h expected %06h (input %06h)", got, m_tdata, expv[got], px[got]);
                end
                if (m_tuser !== (got == 0)) begin bad = bad + 1; $display("    tuser wrong at %0d", got); end
                if (m_tlast !== ((got % LINE) == LINE - 1)) begin bad = bad + 1; $display("    tlast wrong at %0d", got); end
            end
            got = got + 1;
        end
        m_tready <= (($random & 3) != 0);
    end

    // ---- source: random gaps, one frame of N pixels
    task send_frame(input integer which);   // 0 = input itself (bypass), 1 = identity, 2 = full, 3 = half
        integer k, t;
        reg [31:0] r;
        begin
            for (k = 0; k < N; k = k + 1)
                expv[k] = (which == 0) ? px[k] : (which == 1) ? e_ident[k] : (which == 2) ? e_full[k] : e_half[k];
            got = 0; bad = 0; k = 0;
            while (k < N) begin
                @(negedge clk);
                if (($random & 3) != 0) begin
                    s_tvalid = 1; s_tdata = px[k]; s_tuser = (k == 0); s_tlast = ((k % LINE) == LINE - 1);
                    @(posedge clk);
                    if (s_tready) k = k + 1;
                end else s_tvalid = 0;
            end
            @(negedge clk); s_tvalid = 0; s_tuser = 0; s_tlast = 0;
            t = 0;
            while (got < N && t < 5000) begin @(posedge clk); t = t + 1; end
            repeat (5) @(posedge clk);
        end
    endtask

    reg [31:0] r32, expect_sum;
    initial begin
        #200 aresetn = 1;
        repeat (10) @(posedge clk);

        axi_read(16'h0000, r32);                       check("ID register", r32 == 32'h4C555403);
        axi_read(16'h0004, r32);                       check("starts disabled", r32 == 0);
        axi_read(16'h0008, r32);                       check("strength starts at 256", r32 == 256);

        send_frame(0);                                 check("disabled: output equals input (flow control, tuser, tlast)", got == N && bad == 0);

        axi_write(16'h0004, 32'h1);                    // enable
        send_frame(1);                                 check("enabled, power-up identity table matches the Python model", got == N && bad == 0);

        axi_write(16'h0004, 32'h3);                    // clear counters, keep enabled
        for (i = 0; i < 8192; i = i + 1) axi_write(16'h8000 + 4 * i, banks[i]);
        expect_sum = 0;
        for (i = 0; i < 8192; i = i + 1) expect_sum = expect_sum + banks[i][23:0];
        axi_read(16'h0010, r32);                       check("WRITES counts the 8192 words", r32 == 8192);
        axi_read(16'h0014, r32);                       check("CHECKSUM matches", r32 == expect_sum);
        axi_read(16'h8000, r32);                       check("the cube reads back as zero (write only)", r32 == 0);

        send_frame(2);                                 check("random cube, strength 256: every pixel matches the Python model", got == N && bad == 0);

        axi_write(16'h0008, 32'd100);
        send_frame(3);                                 check("random cube, strength 100: every pixel matches the Python model", got == N && bad == 0);

        axi_read(16'h0018, r32);                       check("PROBE_IN is the first pixel", r32[23:0] == px[0]);
        axi_read(16'h001C, r32);                       check("PROBE_OUT is the first output pixel", r32[23:0] == e_half[0]);
        axi_read(16'h000C, r32);                       check("FRAMES counted 4", r32 == 4);

        axi_write(16'h0008, 32'd9999);
        axi_read(16'h0008, r32);                       check("strength saturates at 256", r32 == 256);

        axi_write(16'h0004, 32'h0);                    // bypass again
        send_frame(0);                                 check("disabled again: output equals input", got == N && bad == 0);

        if (errors == 0) $display("TEST PASSED"); else $display("TEST FAILED (%0d)", errors);
        $finish;
    end

    initial begin #200000000; $display("TEST FAILED (timeout)"); $finish; end
endmodule
