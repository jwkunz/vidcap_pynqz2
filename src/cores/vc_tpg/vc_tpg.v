// vc_tpg.v - Free-running test-pattern generator: 8 colour bars plus a moving box.
//
// Produces W x H frames back-to-back on an AXI4-Stream master (tdata = {R,G,B}, tuser = start of
// frame, tlast = end of line). It has no registers and needs no setup; frame rate is set by the
// consumer (vc_input_sel paces it). The pattern is a pure function of the frame number, so tests
// can predict it:
//   * bars (left to right): white, yellow, cyan, green, magenta, red, blue, black
//   * a BOX x BOX box (white with a black border) moves STEP_X pixels right and STEP_Y pixels down
//     per frame, bouncing off the edges, so a frozen or frame-dropping pipeline is obvious.
`timescale 1ns/1ps
module vc_tpg #(
    parameter integer W      = 1280,
    parameter integer H      = 720,
    parameter integer BOX    = 64,
    parameter integer BORDER = 4,
    parameter integer STEP_X = 8,
    parameter integer STEP_Y = 4
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF m_axis, ASSOCIATED_RESET aresetn, FREQ_HZ 100000000" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,
    output wire [23:0] m_axis_tdata,
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire        m_axis_tuser,
    output wire        m_axis_tlast
);
    localparam integer BAR_W = W / 8;

    reg [10:0] x;
    reg [9:0]  y;
    reg [10:0] bx;          // box left edge
    reg [9:0]  by;          // box top edge
    reg        dir_x, dir_y;  // 1 = moving toward larger coordinates

    wire last_x = (x == W - 1);
    wire last_y = (y == H - 1);
    assign m_axis_tvalid = aresetn;
    assign m_axis_tlast  = last_x;
    assign m_axis_tuser  = (x == 0) && (y == 0);

    // colour bars
    reg [23:0] bar;
    // bar index and the position inside the bar are counted along with x, so there is no division in the data path
    reg [2:0]  bar_idx;
    reg [10:0] bar_cnt;
    always @* begin
        case (bar_idx)
            3'd0: bar = 24'hFFFFFF;   // white
            3'd1: bar = 24'hFFFF00;   // yellow  (R+G)
            3'd2: bar = 24'h00FFFF;   // cyan    (G+B)
            3'd3: bar = 24'h00FF00;   // green
            3'd4: bar = 24'hFF00FF;   // magenta (R+B)
            3'd5: bar = 24'hFF0000;   // red
            3'd6: bar = 24'h0000FF;   // blue
            default: bar = 24'h000000; // black
        endcase
    end

    // moving box
    wire in_box = (x >= bx) && (x < bx + BOX) && (y >= by) && (y < by + BOX);
    wire in_border = (x < bx + BORDER) || (x >= bx + BOX - BORDER) ||
                     (y < by + BORDER) || (y >= by + BOX - BORDER);
    assign m_axis_tdata = in_box ? (in_border ? 24'h000000 : 24'hFFFFFF) : bar;

    wire xfer = m_axis_tvalid && m_axis_tready;

    always @(posedge aclk) begin
        if (!aresetn) begin
            x <= 0; y <= 0; bx <= 0; by <= 0; dir_x <= 1'b1; dir_y <= 1'b1; bar_idx <= 3'd0; bar_cnt <= 11'd0;
        end else if (xfer) begin
            if (last_x) begin bar_idx <= 3'd0; bar_cnt <= 11'd0; end
            else if (bar_cnt == BAR_W - 1) begin
                bar_cnt <= 11'd0;
                if (bar_idx != 3'd7) bar_idx <= bar_idx + 3'd1;
            end else bar_cnt <= bar_cnt + 11'd1;
            if (last_x) begin
                x <= 0;
                if (last_y) begin
                    y <= 0;
                    // advance the box for the next frame
                    if (dir_x) begin
                        if (bx + STEP_X + BOX >= W) begin dir_x <= 1'b0; bx <= bx - STEP_X; end
                        else bx <= bx + STEP_X;
                    end else begin
                        if (bx < STEP_X + STEP_X) begin dir_x <= 1'b1; bx <= bx + STEP_X; end   // keep >= 0
                        else bx <= bx - STEP_X;
                    end
                    if (dir_y) begin
                        if (by + STEP_Y + BOX >= H) begin dir_y <= 1'b0; by <= by - STEP_Y; end
                        else by <= by + STEP_Y;
                    end else begin
                        if (by < STEP_Y + STEP_Y) begin dir_y <= 1'b1; by <= by + STEP_Y; end
                        else by <= by - STEP_Y;
                    end
                end else y <= y + 1;
            end else x <= x + 1;
        end
    end
endmodule
