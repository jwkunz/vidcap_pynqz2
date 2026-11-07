// vc_stream_mux.v - Frame-aligned 2:1 AXI4-Stream video mux (live video / video memory).
//
// Selects which of two video readers feeds the filter chain. The selection only changes on a
// frame boundary, and the output always starts on a start-of-frame beat, so downstream sees
// whole frames. The unselected input is stalled (tready = 0), so an idle reader costs no DDR
// traffic.
//
// Register map (byte offsets):
//   0x00 ID      R   0x4D555801
//   0x04 SEL     RW  bit0: 0 = live video, 1 = video memory   (default 0)
//   0x08 STATUS  R   bit0 ACTIVE (source currently being forwarded)   bit1 PENDING (SEL != ACTIVE)
//   0x0C FRAMES  R   frames forwarded
`timescale 1ns/1ps
module vc_stream_mux #(
    parameter integer H = 720
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi:s_axis_live:s_axis_vmem:m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 100000000" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,

    input  wire [3:0]  s_axi_awaddr,  input  wire s_axi_awvalid, output wire s_axi_awready,
    input  wire [31:0] s_axi_wdata,   input  wire [3:0] s_axi_wstrb,
    input  wire        s_axi_wvalid,  output wire s_axi_wready,
    output wire [1:0]  s_axi_bresp,   output wire s_axi_bvalid,  input  wire s_axi_bready,
    input  wire [3:0]  s_axi_araddr,  input  wire s_axi_arvalid, output wire s_axi_arready,
    output wire [31:0] s_axi_rdata,   output wire [1:0] s_axi_rresp,
    output wire        s_axi_rvalid,  input  wire s_axi_rready,

    input  wire [23:0] s_axis_live_tdata, input  wire s_axis_live_tvalid, output wire s_axis_live_tready,
    input  wire        s_axis_live_tuser, input  wire s_axis_live_tlast,
    input  wire [23:0] s_axis_vmem_tdata, input  wire s_axis_vmem_tvalid, output wire s_axis_vmem_tready,
    input  wire        s_axis_vmem_tuser, input  wire s_axis_vmem_tlast,

    output wire [23:0] m_axis_tdata, output wire m_axis_tvalid, input  wire m_axis_tready,
    output wire        m_axis_tuser, output wire m_axis_tlast
);
    localparam [31:0] ID = 32'h4D55_5801;

    // ---- registers
    wire        wr_en;  wire [3:0] wr_addr, rd_addr;  wire [31:0] wr_data;  wire [3:0] wr_strb;
    reg  [31:0] rd_data;
    reg         sel_q;
    reg         active;
    reg  [31:0] frames;

    vc_axil_bridge #(.ADDR_W(4)) u_bridge (
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
        case (rd_addr[3:2])
            2'd0:    rd_data = ID;
            2'd1:    rd_data = {31'd0, sel_q};
            2'd2:    rd_data = {30'd0, (sel_q != active), active};
            default: rd_data = frames;
        endcase
    end

    // ---- datapath
    localparam S_WAIT = 1'b0, S_FWD = 1'b1;
    reg        state;
    reg [10:0] line;

    wire [23:0] s_tdata  = active ? s_axis_vmem_tdata  : s_axis_live_tdata;
    wire        s_tvalid = active ? s_axis_vmem_tvalid : s_axis_live_tvalid;
    wire        s_tuser  = active ? s_axis_vmem_tuser  : s_axis_live_tuser;
    wire        s_tlast  = active ? s_axis_vmem_tlast  : s_axis_live_tlast;

    wire hold_sof = s_tvalid && s_tuser;                       // a start-of-frame we will forward
    wire s_tready = (state == S_FWD) ? m_axis_tready : !hold_sof;   // WAIT: drain up to the SOF

    assign s_axis_live_tready = !active ? s_tready : 1'b0;
    assign s_axis_vmem_tready =  active ? s_tready : 1'b0;
    assign m_axis_tvalid = (state == S_FWD) && s_tvalid;
    assign m_axis_tdata  = s_tdata;
    assign m_axis_tuser  = s_tuser;
    assign m_axis_tlast  = s_tlast;

    wire xfer = m_axis_tvalid && m_axis_tready;

    always @(posedge aclk) begin
        if (!aresetn) begin
            state <= S_WAIT; active <= 1'b0; line <= 0; frames <= 0; sel_q <= 1'b0;
        end else begin
            if (wr_en && wr_addr[3:2] == 2'd1 && wr_strb[0]) sel_q <= wr_data[0];
            case (state)
            S_WAIT: begin
                line <= 0;
                if (active != sel_q) active <= sel_q;          // frame boundary: switch
                else if (hold_sof) state <= S_FWD;
            end
            S_FWD: begin
                if (xfer) begin
                    if (s_tuser) line <= 0;                    // resync on a new SOF
                    if (s_tlast) begin
                        if ((s_tuser ? 0 : line) == H - 1) begin
                            state <= S_WAIT; line <= 0; frames <= frames + 1;
                        end else line <= (s_tuser ? 0 : line) + 1;
                    end
                end
            end
            endcase
        end
    end
endmodule
