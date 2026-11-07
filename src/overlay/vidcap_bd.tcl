# vidcap_bd.tcl - Block design for the vidcap overlay (PYNQ-Z2, xc7z020).
#
# Expects (set by build.tcl):  root  = repository root,  refs = workspace/vivado/refs/PYNQ
#
# Clocking (see docs/requirements.md):
#   aclk   100 MHz  PS FCLK0 - every stream core, VDMA, interconnect and all AXI-Lite registers
#   200 MHz         PS FCLK1 - IDELAY reference for the HDMI receiver
#   pixel  74.25 MHz (MMCM, exact) - HDMI output pixel clock; the HDMI input clock is recovered
# The only clock crossings are inside Xilinx IP: v_vid_in_axi4s (HDMI clock -> aclk) and
# v_axi4s_vid_out (aclk -> pixel clock).
#
# Pixel format in the pipeline is {R,G,B}; the Digilent HDMI IP uses {R,B,G}, swapped once at
# each edge (inside vc_input_sel on input, vc_axis_swap_gb on output).

source $root/src/overlay/scripts/ps7_preset.tcl

# ---------------------------------------------------------------- helpers
proc cip {vlnv name args} {
    set c [create_bd_cell -type ip -vlnv $vlnv $name]
    if {[llength $args]} { set_property -dict $args [get_bd_cells $name] }
    return $c
}
proc cmod {ref name} { return [create_bd_cell -type module -reference $ref $name] }
proc net {src args} { foreach d $args { connect_bd_net [get_bd_pins $src] [get_bd_pins $d] } }
proc intf {a b} { connect_bd_intf_net [get_bd_intf_pins $a] [get_bd_intf_pins $b] }

# ---------------------------------------------------------------- Processing system
set ps7 [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7]
apply_pynqz2_ps7 $ps7 $refs/boards/Pynq-Z2/base/base.tcl
set_property -dict [list \
    CONFIG.PCW_FPGA0_PERIPHERAL_FREQMHZ {100} \
    CONFIG.PCW_FPGA1_PERIPHERAL_FREQMHZ {200} \
    CONFIG.PCW_EN_CLK2_PORT {0} CONFIG.PCW_EN_CLK3_PORT {0} \
    CONFIG.PCW_FPGA_FCLK2_ENABLE {0} CONFIG.PCW_FPGA_FCLK3_ENABLE {0} \
    CONFIG.PCW_USE_M_AXI_GP0 {1} CONFIG.PCW_USE_M_AXI_GP1 {0} \
    CONFIG.PCW_USE_S_AXI_GP0 {0} CONFIG.PCW_USE_S_AXI_HP0 {1} CONFIG.PCW_USE_S_AXI_HP1 {1} \
    CONFIG.PCW_USE_S_AXI_HP2 {0} CONFIG.PCW_USE_S_AXI_HP3 {0} \
    CONFIG.PCW_USE_FABRIC_INTERRUPT {1} CONFIG.PCW_IRQ_F2P_INTR {1} \
] [get_bd_cells ps7]

set rst [cip xilinx.com:ip:proc_sys_reset:5.0 rst]
net ps7/FCLK_CLK0 rst/slowest_sync_clk
net ps7/FCLK_RESET0_N rst/ext_reset_in
# aclk = ps7/FCLK_CLK0 ; aresetn = rst/peripheral_aresetn

set c1 [cip xilinx.com:ip:xlconstant:1.1 c1 CONFIG.CONST_VAL {1} CONFIG.CONST_WIDTH {1}]

# ---------------------------------------------------------------- Pixel clock (74.25 MHz exactly)
set clkw [cip xilinx.com:ip:clk_wiz:6.0 clkw \
    CONFIG.PRIM_IN_FREQ {100.000} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {74.250} \
    CONFIG.USE_RESET {false} \
    CONFIG.USE_LOCKED {true}]
net ps7/FCLK_CLK0 clkw/clk_in1
# pixel-domain reset (active-low out of proc_sys_reset; active-high version for the video IP)
set prst [cip xilinx.com:ip:proc_sys_reset:5.0 prst]
net clkw/clk_out1 prst/slowest_sync_clk
net clkw/locked prst/ext_reset_in

# ---------------------------------------------------------------- HDMI input
set hdmi_in [create_bd_intf_port -mode Slave -vlnv digilentinc.com:interface:tmds_rtl:1.0 hdmi_in]
set hdmi_in_ddc [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:iic_rtl:1.0 hdmi_in_ddc]
create_bd_port -dir O -from 0 -to 0 hdmi_in_hpd
create_bd_port -dir O -from 0 -to 0 hdmi_out_hpd
net c1/dout hdmi_in_hpd
net c1/dout hdmi_out_hpd

set dvi [cip digilentinc.com:ip:dvi2rgb:1.7 dvi \
    CONFIG.kEdidFileName {720p_edid.data} \
    CONFIG.kClkRange {2} \
    CONFIG.kRstActiveHigh {false} \
    CONFIG.kAddBUFG {false}]
connect_bd_intf_net $hdmi_in [get_bd_intf_pins dvi/TMDS]
connect_bd_intf_net $hdmi_in_ddc [get_bd_intf_pins dvi/DDC]
net ps7/FCLK_CLK1 dvi/RefClk
net rst/peripheral_aresetn dvi/aRst_n

# recovered-pixel-clock domain reset for the video-in converter
set vrst [cip xilinx.com:ip:proc_sys_reset:5.0 vrst]
net dvi/PixelClk vrst/slowest_sync_clk
net rst/peripheral_aresetn vrst/ext_reset_in
net dvi/aPixelClkLckd vrst/dcm_locked

set vin [cip xilinx.com:ip:v_vid_in_axi4s:5.0 vin \
    CONFIG.C_ADDR_WIDTH {12} \
    CONFIG.C_HAS_ASYNC_CLK {1}]
intf dvi/RGB vin/vid_io_in
net dvi/PixelClk vin/vid_io_in_clk
net vrst/peripheral_reset vin/vid_io_in_reset
net c1/dout vin/vid_io_in_ce vin/aclken
net ps7/FCLK_CLK0 vin/aclk
net rst/peripheral_aresetn vin/aresetn

# ---------------------------------------------------------------- Stream cores
set tpg  [cmod vc_tpg tpg]
set isel [cmod vc_input_sel isel]
set snap [cip vidcap:vidcap:vc_snapshot:1.0 snap]
set vmx  [cmod vc_stream_mux vmx]
set filt [cip vidcap:vidcap:vc_filter:1.0 filt]
set ovl  [cip vidcap:vidcap:vc_overlay:1.0 ovl]
set swp  [cmod vc_axis_swap_gb swp]
set rtc  [cmod vc_rtc rtc]

# input select: HDMI + test pattern -> snapshot -> VDMA write
intf vin/video_out isel/s_axis_hdmi
intf tpg/m_axis isel/s_axis_tpg
net dvi/aPixelClkLckd isel/hdmi_locked
intf isel/m_axis snap/in_r

# VDMA 0: live video ring (S2MM writes, MM2S reads), 3 frame stores. Genlock: S2MM = master, MM2S =
# slave (static) - the same combination as PYNQ's own base design - so the reader shows the frame
# the writer last completed and never one that is being written.
set vdma0 [cip xilinx.com:ip:axi_vdma:6.3 vdma0 \
    CONFIG.c_num_fstores {3} \
    CONFIG.c_include_mm2s {1} CONFIG.c_include_s2mm {1} \
    CONFIG.c_s2mm_genlock_mode {0} CONFIG.c_mm2s_genlock_mode {1} \
    CONFIG.c_m_axis_mm2s_tdata_width {24} \
    CONFIG.c_m_axi_mm2s_data_width {64} CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_s2mm_linebuffer_depth {1024} CONFIG.c_mm2s_linebuffer_depth {1024} \
    CONFIG.c_mm2s_max_burst_length {16} CONFIG.c_s2mm_max_burst_length {16}]
# VDMA 1: video memory (read only), 2 frame stores
set vdma1 [cip xilinx.com:ip:axi_vdma:6.3 vdma1 \
    CONFIG.c_num_fstores {2} \
    CONFIG.c_include_mm2s {1} CONFIG.c_include_s2mm {0} \
    CONFIG.c_mm2s_genlock_mode {0} \
    CONFIG.c_m_axis_mm2s_tdata_width {24} \
    CONFIG.c_m_axi_mm2s_data_width {64} \
    CONFIG.c_mm2s_linebuffer_depth {1024} \
    CONFIG.c_mm2s_max_burst_length {16}]

intf snap/out_r vdma0/S_AXIS_S2MM
intf vdma0/M_AXIS_MM2S vmx/s_axis_live
intf vdma1/M_AXIS_MM2S vmx/s_axis_vmem
intf vmx/m_axis filt/in_r
intf filt/out_r ovl/in_r
intf ovl/out_r swp/s_axis
net rtc/time_digits ovl/time_digits

# ---------------------------------------------------------------- HDMI output
set vtc [cip xilinx.com:ip:v_tc:6.2 vtc \
    CONFIG.HAS_AXI4_LITE {false} \
    CONFIG.enable_detection {false} \
    CONFIG.enable_generation {true} \
    CONFIG.VIDEO_MODE {720p}]
set vout [cip xilinx.com:ip:v_axi4s_vid_out:4.0 vout \
    CONFIG.C_ADDR_WIDTH {11} \
    CONFIG.C_HAS_ASYNC_CLK {1} \
    CONFIG.C_HYSTERESIS_LEVEL {1024} \
    CONFIG.C_VTG_MASTER_SLAVE {1}]
set rgb [cip digilentinc.com:ip:rgb2dvi:1.2 rgb \
    CONFIG.kClkRange {2} \
    CONFIG.kGenerateSerialClk {true} \
    CONFIG.kRstActiveHigh {false}]
set hdmi_out [create_bd_intf_port -mode Master -vlnv digilentinc.com:interface:tmds_rtl:1.0 hdmi_out]
connect_bd_intf_net [get_bd_intf_pins rgb/TMDS] $hdmi_out

intf swp/m_axis vout/video_in
intf vtc/vtiming_out vout/vtiming_in
intf vout/vid_io_out rgb/RGB
net ps7/FCLK_CLK0 vout/aclk
net rst/peripheral_aresetn vout/aresetn
net c1/dout vout/aclken vout/vid_io_out_ce vtc/clken
net clkw/clk_out1 vout/vid_io_out_clk vtc/clk rgb/PixelClk
net prst/peripheral_reset vout/vid_io_out_reset
net prst/peripheral_aresetn vtc/resetn
net clkw/locked rgb/aRst_n
net vout/vtg_ce vtc/gen_clken

# ---------------------------------------------------------------- Status LEDs
create_bd_port -dir O -from 3 -to 0 leds
set ledcat [cip xilinx.com:ip:xlconcat:2.1 ledcat CONFIG.NUM_PORTS {4}]
net isel/st_hdmi_good ledcat/In0
net isel/st_fallback ledcat/In1
net isel/st_src_tpg ledcat/In2
net clkw/locked ledcat/In3
net ledcat/dout leds

# ---------------------------------------------------------------- Interrupts
# bit 0 = snapshot done, bit 1 = VDMA0 S2MM, bit 2 = VDMA0 MM2S, bit 3 = VDMA1 MM2S.
# The HLS irq pin is an untyped data pin and the VDMA pins are typed interrupts; xlconcat refuses
# to mix them, so the three VDMA lines go through a slice (which makes them plain data).
set vcat [cip xilinx.com:ip:xlconcat:2.1 vcat CONFIG.NUM_PORTS {3}]
net vdma0/s2mm_introut vcat/In0
net vdma0/mm2s_introut vcat/In1
net vdma1/mm2s_introut vcat/In2
set vsl [cip xilinx.com:ip:xlslice:1.0 vsl CONFIG.DIN_WIDTH {3} CONFIG.DIN_FROM {2} CONFIG.DIN_TO {0} CONFIG.DOUT_WIDTH {3}]
net vcat/dout vsl/Din
set irqcat [cip xilinx.com:ip:xlconcat:2.1 irqcat CONFIG.NUM_PORTS {2} CONFIG.IN0_WIDTH {1} CONFIG.IN1_WIDTH {3}]
net snap/irq irqcat/In0
net vsl/Dout irqcat/In1
set intc [cip xilinx.com:ip:axi_intc:4.1 intc \
    CONFIG.C_IRQ_IS_LEVEL {1} \
    CONFIG.C_KIND_OF_INTR {0x00000000}]
net irqcat/dout intc/intr
net intc/irq ps7/IRQ_F2P

# ---------------------------------------------------------------- AXI infrastructure
# control: PS GP0 -> 9 AXI-Lite slaves
set ic_gp [cip xilinx.com:ip:axi_interconnect:2.1 ic_gp CONFIG.NUM_SI {1} CONFIG.NUM_MI {9}]
intf ps7/M_AXI_GP0 ic_gp/S00_AXI
set slaves {vdma0/S_AXI_LITE vdma1/S_AXI_LITE rtc/s_axi isel/s_axi vmx/s_axi
            filt/s_axi_control ovl/s_axi_control snap/s_axi_control intc/s_axi}
set i 0
foreach s $slaves { intf ic_gp/M0${i}_AXI $s; incr i }

# data: VDMA0 (2 masters) -> HP0 ; VDMA1 + snapshot -> HP1
set ic_hp0 [cip xilinx.com:ip:axi_interconnect:2.1 ic_hp0 CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}]
set ic_hp1 [cip xilinx.com:ip:axi_interconnect:2.1 ic_hp1 CONFIG.NUM_SI {2} CONFIG.NUM_MI {1}]
intf vdma0/M_AXI_S2MM ic_hp0/S00_AXI
intf vdma0/M_AXI_MM2S ic_hp0/S01_AXI
intf ic_hp0/M00_AXI ps7/S_AXI_HP0
intf vdma1/M_AXI_MM2S ic_hp1/S00_AXI
intf snap/m_axi_gmem ic_hp1/S01_AXI
intf ic_hp1/M00_AXI ps7/S_AXI_HP1

# ---------------------------------------------------------------- clocks and resets on everything at aclk
set aclk_pins {
    ps7/M_AXI_GP0_ACLK ps7/S_AXI_HP0_ACLK ps7/S_AXI_HP1_ACLK
    ic_gp/ACLK ic_gp/S00_ACLK
    ic_hp0/ACLK ic_hp0/S00_ACLK ic_hp0/S01_ACLK ic_hp0/M00_ACLK
    ic_hp1/ACLK ic_hp1/S00_ACLK ic_hp1/S01_ACLK ic_hp1/M00_ACLK
    vdma0/s_axi_lite_aclk vdma0/m_axi_mm2s_aclk vdma0/m_axi_s2mm_aclk vdma0/m_axis_mm2s_aclk vdma0/s_axis_s2mm_aclk
    vdma1/s_axi_lite_aclk vdma1/m_axi_mm2s_aclk vdma1/m_axis_mm2s_aclk
    snap/ap_clk filt/ap_clk ovl/ap_clk
    tpg/aclk isel/aclk vmx/aclk swp/aclk rtc/aclk intc/s_axi_aclk
}
foreach p $aclk_pins { net ps7/FCLK_CLK0 $p }
set i 0
foreach s $slaves { net ps7/FCLK_CLK0 ic_gp/M0${i}_ACLK; net rst/peripheral_aresetn ic_gp/M0${i}_ARESETN; incr i }

set rst_pins {
    ic_gp/S00_ARESETN
    ic_hp0/S00_ARESETN ic_hp0/S01_ARESETN ic_hp0/M00_ARESETN
    ic_hp1/S00_ARESETN ic_hp1/S01_ARESETN ic_hp1/M00_ARESETN
    vdma0/axi_resetn vdma1/axi_resetn
    snap/ap_rst_n filt/ap_rst_n ovl/ap_rst_n
    tpg/aresetn isel/aresetn vmx/aresetn rtc/aresetn intc/s_axi_aresetn
}
foreach p $rst_pins { net rst/peripheral_aresetn $p }
net rst/interconnect_aresetn ic_gp/ARESETN ic_hp0/ARESETN ic_hp1/ARESETN

# ---------------------------------------------------------------- address map (see docs/register_map.md)
assign_bd_address
set map {vdma0 0x40000000 vdma1 0x40010000 rtc 0x40020000 isel 0x40030000 vmx 0x40040000
         filt 0x40050000 ovl 0x40060000 snap 0x40070000 intc 0x40080000}
# pass 1 parks every register block at a temporary address so pass 2 can never collide with the
# addresses the automatic assignment picked; pass 2 sets the final, documented addresses.
set k 0
foreach {cell off} $map {
    set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces ps7/Data] -filter "NAME =~ SEG_${cell}_*"]
    if {[llength $seg] != 1} { error "expected one address segment for $cell, found [llength $seg]" }
    set_property offset [format 0x%08X [expr {0x60000000 + $k * 0x10000}]] $seg
    incr k
}
foreach {cell off} $map {
    set seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces ps7/Data] -filter "NAME =~ SEG_${cell}_*"]
    set_property offset $off $seg
}
