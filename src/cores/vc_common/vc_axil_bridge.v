// vc_axil_bridge.v - AXI4-Lite slave -> simple register bus.
//
// One outstanding write and one outstanding read at a time. The core behind it
// supplies `rd_data` combinationally from `rd_addr` (sampled in the cycle the read
// address is accepted), and consumes `wr_en/wr_addr/wr_data/wr_strb` strobes.
// Addresses are byte addresses (ADDR_W bits).
`timescale 1ns/1ps
module vc_axil_bridge #(
    parameter ADDR_W = 6
) (
    input  wire              aclk,
    input  wire              aresetn,

    input  wire [ADDR_W-1:0] s_axi_awaddr,
    input  wire              s_axi_awvalid,
    output reg               s_axi_awready,
    input  wire [31:0]       s_axi_wdata,
    input  wire [3:0]        s_axi_wstrb,
    input  wire              s_axi_wvalid,
    output reg               s_axi_wready,
    output reg  [1:0]        s_axi_bresp,
    output reg               s_axi_bvalid,
    input  wire              s_axi_bready,
    input  wire [ADDR_W-1:0] s_axi_araddr,
    input  wire              s_axi_arvalid,
    output reg               s_axi_arready,
    output reg  [31:0]       s_axi_rdata,
    output reg  [1:0]        s_axi_rresp,
    output reg               s_axi_rvalid,
    input  wire              s_axi_rready,

    // register bus
    output reg               wr_en,
    output reg  [ADDR_W-1:0] wr_addr,
    output reg  [31:0]       wr_data,
    output reg  [3:0]        wr_strb,
    output wire [ADDR_W-1:0] rd_addr,
    input  wire [31:0]       rd_data
);
    reg              aw_got, w_got;
    reg [ADDR_W-1:0] awaddr_q;
    reg [31:0]       wdata_q;
    reg [3:0]        wstrb_q;

    // ---- write channel ----
    always @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_awready <= 1'b0; s_axi_wready <= 1'b0;
            s_axi_bvalid  <= 1'b0; s_axi_bresp  <= 2'b00;
            aw_got <= 1'b0; w_got <= 1'b0; wr_en <= 1'b0;
            awaddr_q <= {ADDR_W{1'b0}}; wdata_q <= 32'd0; wstrb_q <= 4'd0;
            wr_addr <= {ADDR_W{1'b0}}; wr_data <= 32'd0; wr_strb <= 4'd0;
        end else begin
            wr_en <= 1'b0;
            s_axi_awready <= 1'b0;
            s_axi_wready  <= 1'b0;

            if (s_axi_awvalid && !aw_got && !s_axi_bvalid && !s_axi_awready) begin
                s_axi_awready <= 1'b1; aw_got <= 1'b1; awaddr_q <= s_axi_awaddr;
            end
            if (s_axi_wvalid && !w_got && !s_axi_bvalid && !s_axi_wready) begin
                s_axi_wready <= 1'b1; w_got <= 1'b1;
                wdata_q <= s_axi_wdata; wstrb_q <= s_axi_wstrb;
            end
            if (aw_got && w_got && !s_axi_bvalid) begin
                wr_en <= 1'b1; wr_addr <= awaddr_q; wr_data <= wdata_q; wr_strb <= wstrb_q;
                s_axi_bvalid <= 1'b1; s_axi_bresp <= 2'b00;
                aw_got <= 1'b0; w_got <= 1'b0;
            end
            if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 1'b0;
        end
    end

    // ---- read channel ----
    assign rd_addr = s_axi_araddr;
    always @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_arready <= 1'b0; s_axi_rvalid <= 1'b0;
            s_axi_rdata <= 32'd0;  s_axi_rresp <= 2'b00;
        end else begin
            s_axi_arready <= 1'b0;
            if (s_axi_arvalid && !s_axi_rvalid && !s_axi_arready) begin
                s_axi_arready <= 1'b1;
                s_axi_rdata   <= rd_data;
                s_axi_rresp   <= 2'b00;
                s_axi_rvalid  <= 1'b1;
            end else if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0;
            end
        end
    end
endmodule
