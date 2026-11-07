# build.tcl - Build the vidcap overlay from source.
#
#   vivado -mode batch -source src/overlay/scripts/build.tcl -tclargs <repo_root> [stage] [mode]
#
#   stage = bd      create the project and block design, validate it, stop
#           synth   ... and run synthesis
#           impl    ... and run implementation
#           bit     ... and write the bitstream and export (default, runs everything)
#   mode  = 720p (default) | 480p | 240p : the video mode, defined in src/overlay/modes/<mode>.env. Each mode is a
#           separate build with its own project, IP repository and output: out_<mode>/vidcap_<mode>.bit/.hwh
#
# Everything is generated under workspace/vivado/ (untracked). Prerequisites, in order:
#   src/overlay/scripts/fetch_refs.sh                  (PYNQ-Z2 PS settings, HDMI IP; also runs prepare_ip.py)
#   MODE=<mode> src/cores/vc_common/run_hls.sh <core> package   (vc_filter, vc_overlay, vc_snapshot, vc_tavg)
#   src/overlay/scripts/collect_ip.sh <mode>           (unpack the HLS IPs)
set root  [lindex $argv 0]
set stage [expr {[llength $argv] > 1 ? [lindex $argv 1] : "bit"}]
set mode  [expr {[llength $argv] > 2 ? [lindex $argv 2] : "720p"}]
set ws    $root/workspace/vivado
set refs  $ws/refs/PYNQ
set proj  $ws/vidcap_proj_$mode
set out   $ws/out_$mode

# the video mode: frame size, pixel clock, receiver band, EDID and output timing (one file per mode)
proc load_mode {file} {
    set d [dict create]
    set fh [open $file r]
    foreach line [split [read $fh] "\n"] {
        if {[regexp {^([A-Z_0-9]+)=(\S+)} $line -> k v]} { dict set d $k $v }
    }
    close $fh
    return $d
}
set modefile $root/src/overlay/modes/$mode.env
if {![file exists $modefile]} { error "unknown mode '$mode' (no $modefile)" }
set cfg [load_mode $modefile]
puts "MODE $mode: [dict get $cfg WIDTH]x[dict get $cfg HEIGHT], pixel clock [dict get $cfg PIXEL_MHZ] MHz, receiver band [dict get $cfg RX_CLK_RANGE], EDID [dict get $cfg EDID]"

proc banner {msg} { puts "\n===== $msg =====" }

foreach d [list $refs/boards/Pynq-Z2/base/base.tcl $ws/ip_local/dvi2rgb_v1_7 $ws/ip_repo_$mode/vc_filter $ws/ip_repo_$mode/vc_overlay $ws/ip_repo_$mode/vc_snapshot $ws/ip_repo_$mode/vc_tavg] {
    if {![file exists $d]} { error "missing prerequisite: $d (see the header of this script)" }
}

banner "project"
file delete -force $proj
create_project vidcap $proj -part xc7z020clg400-1
set_property target_language Verilog [current_project]
set_property ip_repo_paths [list $ws/ip_repo_$mode $ws/ip_local] [current_project]
update_ip_catalog

set rtl [list \
    $root/src/cores/vc_common/vc_axil_bridge.v \
    $root/src/cores/vc_rtc/vc_rtc.v \
    $root/src/cores/vc_input_sel/vc_input_sel.v \
    $root/src/cores/vc_tpg/vc_tpg.v \
    $root/src/cores/vc_stream_mux/vc_stream_mux.v \
    $root/src/cores/vc_axis_swap/vc_axis_swap_gb.v \
    $root/src/cores/vc_lut3d/vc_lut3d.v]
add_files -norecurse $rtl
# The HDMI input clock constraint follows the mode: vidcap.xdc is written for 120 MHz, so a per-mode copy gets the
# mode's own clock (RX_CONSTRAIN_MHZ, default PIXEL_MHZ). A wrong value makes the receiver MMCM fail its VCO check.
set rxmhz [expr {[dict exists $cfg RX_CONSTRAIN_MHZ] ? [dict get $cfg RX_CONSTRAIN_MHZ] : [dict get $cfg PIXEL_MHZ]}]
set per [format %.3f [expr {1000.0 / $rxmhz}]]
set xf [open $root/src/overlay/vidcap.xdc r]; set xtxt [read $xf]; close $xf
set xtxt [string map [list "create_clock -period 8.334 -name hdmi_in_clk -waveform {0.000 4.167}" \
    "create_clock -period $per -name hdmi_in_clk -waveform {0.000 [format %.3f [expr {$per / 2.0}]]}"] $xtxt]
file mkdir $proj/constr
set xf [open $proj/constr/vidcap_$mode.xdc w]; puts -nonewline $xf $xtxt; close $xf
add_files -fileset constrs_1 -norecurse $proj/constr/vidcap_$mode.xdc
update_compile_order -fileset sources_1

banner "block design"
create_bd_design vidcap
source $root/src/overlay/vidcap_bd.tcl
regenerate_bd_layout
save_bd_design
set verr [catch {validate_bd_design} vmsg]
puts "validate_bd_design: $vmsg"
if {$verr} { error "block design did not validate" }
save_bd_design

make_wrapper -files [get_files $proj/vidcap.srcs/sources_1/bd/vidcap/vidcap.bd] -top
add_files -norecurse $proj/vidcap.gen/sources_1/bd/vidcap/hdl/vidcap_wrapper.v
set_property top vidcap_wrapper [current_fileset]
update_compile_order -fileset sources_1
if {$stage eq "bd"} { puts "stopping after block design"; exit 0 }

file mkdir $out
banner "synthesis"
set_property STEPS.SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY rebuilt [get_runs synth_1]
launch_runs synth_1 -jobs 12
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { error "synthesis failed" }
open_run synth_1
report_utilization -file $out/utilization_synth.rpt
report_clock_interaction -file $out/clock_interaction_synth.rpt
report_clocks -file $out/clocks_synth.rpt
close_design
if {$stage eq "synth"} { exit 0 }

banner "implementation"
launch_runs impl_1 -to_step route_design -jobs 12
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { error "implementation failed" }
open_run impl_1
report_timing_summary -delay_type min_max -report_unconstrained -max_paths 20 -file $out/timing_summary.rpt
report_clock_interaction -file $out/clock_interaction_impl.rpt
report_utilization -file $out/utilization_impl.rpt
report_power -file $out/power.rpt
report_drc -file $out/drc.rpt
# worst setup / hold slack over every constrained path
set wns [get_property SLACK [lindex [get_timing_paths -max_paths 1 -nworst 1 -setup] 0]]
set whs [get_property SLACK [lindex [get_timing_paths -max_paths 1 -nworst 1 -hold] 0]]
puts "TIMING: WNS=$wns WHS=$whs"
set timing_ok [expr {$wns >= 0 && $whs >= 0}]
close_design
if {!$timing_ok && ![info exists ::env(VIDCAP_ALLOW_TIMING_FAIL)]} {
    error "timing not met (WNS=$wns WHS=$whs); see $out/timing_summary.rpt. Set VIDCAP_ALLOW_TIMING_FAIL=1 to build anyway."
}
if {$stage eq "impl"} { exit 0 }

banner "bitstream"
launch_runs impl_1 -to_step write_bitstream -jobs 12
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { error "bitstream generation failed" }
file copy -force $proj/vidcap.runs/impl_1/vidcap_wrapper.bit $out/vidcap_$mode.bit
file copy -force $proj/vidcap.gen/sources_1/bd/vidcap/hw_handoff/vidcap.hwh $out/vidcap_$mode.hwh
puts "WROTE $out/vidcap_$mode.bit and $out/vidcap_$mode.hwh"
puts "TIMING: WNS=$wns WHS=$whs"
