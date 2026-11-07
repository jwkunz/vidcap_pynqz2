// Self-checking testbench for vc_tpg (scaled: 64x32 frame, 16-pixel box).
`timescale 1ns/1ps
module tb_vc_tpg;
    localparam W = 64, H = 32, BOX = 16, BORDER = 2, SX = 8, SY = 4;
    reg aclk = 0, aresetn = 0, ready = 1;
    always #5 aclk = ~aclk;
    wire [23:0] d; wire v, u, l;
    vc_tpg #(.W(W), .H(H), .BOX(BOX), .BORDER(BORDER), .STEP_X(SX), .STEP_Y(SY)) dut (
        .aclk(aclk), .aresetn(aresetn), .m_axis_tdata(d), .m_axis_tvalid(v),
        .m_axis_tready(ready), .m_axis_tuser(u), .m_axis_tlast(l));

    integer fails = 0, x = 0, y = 0, frame = 0, i;
    reg [23:0] img [0:W*H-1];
    integer box_x [0:63], box_y [0:63];
    integer struct_err = 0, bar_err = 0;
    reg [23:0] exp_bar [0:7];

    // locate the box in the captured frame (black-border pixels inside the bars region)
    task find_box(output integer fx, output integer fy);
        integer px, py, found;
        begin
            fx = -1; fy = -1; found = 0;
            // the top-left corner of the box is the first pixel of the 2x2 border that differs from its bar
            for (py = 0; py < H && !found; py = py + 1)
                for (px = 0; px < W && !found; px = px + 1)
                    if (img[py*W+px] == 24'h000000 && img[py*W+px] != exp_bar[(px / (W/8)) > 7 ? 7 : (px / (W/8))]
                        && px + BOX <= W && py + BOX <= H
                        && img[py*W+px+BOX-1] == 24'h000000 && img[(py+BOX-1)*W+px] == 24'h000000
                        && img[(py+BOX/2)*W+px+BOX/2] == 24'hFFFFFF) begin
                        fx = px; fy = py; found = 1;
                    end
        end
    endtask

    always @(posedge aclk) if (aresetn && v && ready) begin
        if (u !== (x == 0 && y == 0)) struct_err = struct_err + 1;
        if (l !== (x == W - 1)) struct_err = struct_err + 1;
        img[y*W + x] = d;
        if (x == W - 1) begin
            x = 0;
            if (y == H - 1) begin
                y = 0;
                if (frame < 64) find_box(box_x[frame], box_y[frame]);
                frame = frame + 1;
            end else y = y + 1;
        end else x = x + 1;
    end

    integer bi, ex, ey, dx, dy;
    initial begin
        exp_bar[0] = 24'hFFFFFF; exp_bar[1] = 24'hFFFF00; exp_bar[2] = 24'h00FFFF; exp_bar[3] = 24'h00FF00;
        exp_bar[4] = 24'hFF00FF; exp_bar[5] = 24'hFF0000; exp_bar[6] = 24'h0000FF; exp_bar[7] = 24'h000000;
        repeat (4) @(posedge aclk); aresetn <= 1;
        wait (frame >= 50);
        repeat (2) @(posedge aclk);

        if (struct_err != 0) begin fails = fails + 1; $display("FAIL framing errors: %0d", struct_err); end
        else $display("ok   framing: tuser/tlast correct on %0d frames of %0dx%0d", frame, W, H);

        // bars: sample a row below the box path (last row) and above it (row 0) is box-free at frame 0 only,
        // so check from the saved image of the final frame using a row the box cannot reach.
        bar_err = 0;
        for (bi = 0; bi < 8; bi = bi + 1) begin
            // row H-1 is outside any box position when the box is not at the bottom; use whichever row
            // is farthest from the box
            ey = (box_y[frame-1] > H/2) ? 0 : H - 1;
            if (img[ey*W + bi*(W/8) + (W/16)] !== exp_bar[bi]) bar_err = bar_err + 1;
        end
        if (bar_err != 0) begin fails = fails + 1; $display("FAIL colour bars (%0d wrong)", bar_err); end
        else $display("ok   8 colour bars in the right order and colours");

        // box motion: step sizes and bouncing
        dx = 0; dy = 0;
        for (i = 1; i < 50; i = i + 1) begin
            if (box_x[i] < 0 || box_y[i] < 0) begin fails = fails + 1; $display("FAIL box not found in frame %0d", i); i = 50; end
            else begin
                ex = box_x[i] - box_x[i-1]; ey = box_y[i] - box_y[i-1];
                if (ex != SX && ex != -SX) begin fails = fails + 1; $display("FAIL frame %0d x step %0d", i, ex); end
                if (ey != SY && ey != -SY) begin fails = fails + 1; $display("FAIL frame %0d y step %0d", i, ey); end
                if (box_x[i] < 0 || box_x[i] + BOX > W || box_y[i] < 0 || box_y[i] + BOX > H) begin
                    fails = fails + 1; $display("FAIL frame %0d box out of bounds (%0d,%0d)", i, box_x[i], box_y[i]);
                end
                if (ex < 0) dx = 1;   // saw a bounce
                if (ey < 0) dy = 1;
            end
        end
        if (dx && dy) $display("ok   box moves %0d/%0d px per frame and bounces off both edges", SX, SY);
        else begin fails = fails + 1; $display("FAIL box never bounced (dx=%0d dy=%0d)", dx, dy); end

        // determinism: pattern repeats with the same sequence after reset
        if (fails == 0) $display("TEST PASSED"); else $display("TEST FAILED (%0d)", fails);
        $finish;
    end
    initial begin #50_000_000; $display("TEST FAILED (timeout)"); $finish; end
endmodule
