// vc_axis_swap_gb.v - Swap the lower two bytes of a 24-bit AXI4-Stream (pure wiring).
//
// The pipeline uses {R,G,B}; the Digilent rgb2dvi encoder wants {R,B,G}. Placed on the output
// path just before v_axi4s_vid_out. No state, no clock.
`timescale 1ns/1ps
module vc_axis_swap_gb (
    // clock is unused (pure wiring); it exists so Vivado can associate the stream interfaces with a clock
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axis:m_axis, FREQ_HZ 100000000" *)
    input  wire        aclk,
    input  wire [23:0] s_axis_tdata,
    input  wire        s_axis_tvalid,
    output wire        s_axis_tready,
    input  wire        s_axis_tuser,
    input  wire        s_axis_tlast,
    output wire [23:0] m_axis_tdata,
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire        m_axis_tuser,
    output wire        m_axis_tlast
);
    assign m_axis_tdata  = {s_axis_tdata[23:16], s_axis_tdata[7:0], s_axis_tdata[15:8]};
    assign m_axis_tvalid = s_axis_tvalid;
    assign s_axis_tready = m_axis_tready;
    assign m_axis_tuser  = s_axis_tuser;
    assign m_axis_tlast  = s_axis_tlast;
endmodule
