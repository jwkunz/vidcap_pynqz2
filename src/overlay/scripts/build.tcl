# build.tcl - Build the vidcap overlay from source.
#
#   vivado -mode batch -source src/overlay/scripts/build.tcl -tclargs <repo_root> [stage]
#
#   stage = bd      create the project and block design, validate it, stop
#           synth   ... and run synthesis
#           impl    ... and run implementation
#           bit     ... and write the bitstream and export (default, runs everything)
#
# Everything is generated under workspace/vivado/ (untracked). Prerequisites, in order:
#   src/overlay/scripts/fetch_refs.sh                  (PYNQ-Z2 PS settings, HDMI IP)
#   src/cores/vc_common/run_hls.sh <core> package      (vc_filter, vc_overlay, vc_snapshot)
#   src/overlay/scripts/collect_ip.sh                  (unpack the HLS IPs)
set root  [lindex $argv 0]
set stage [expr {[llength $argv] > 1 ? [lindex $argv 1] : "bit"}]
set ws    $root/workspace/vivado
set refs  $ws/refs/PYNQ
set proj  $ws/vidcap_proj
set out   $ws/out

proc banner {msg} { puts "\n===== $msg =====" }

foreach d [list $refs/boards/Pynq-Z2/base/base.tcl $ws/ip_repo/vc_filter $ws/ip_repo/vc_overlay $ws/ip_repo/vc_snapshot $ws/ip_repo/vc_tavg] {
    if {![file exists $d]} { error "missing prerequisite: $d (see the header of this script)" }
}

banner "project"
file delete -force $proj
create_project vidcap $proj -part xc7z020clg400-1
set_property target_language Verilog [current_project]
set_property ip_repo_paths [list $ws/ip_repo $refs/boards/ip] [current_project]
update_ip_catalog

set rtl [list \
    $root/src/cores/vc_common/vc_axil_bridge.v \
    $root/src/cores/vc_rtc/vc_rtc.v \
    $root/src/cores/vc_input_sel/vc_input_sel.v \
    $root/src/cores/vc_tpg/vc_tpg.v \
    $root/src/cores/vc_stream_mux/vc_stream_mux.v \
    $root/src/cores/vc_axis_swap/vc_axis_swap_gb.v]
add_files -norecurse $rtl
add_files -fileset constrs_1 -norecurse $root/src/overlay/vidcap.xdc
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
file copy -force $proj/vidcap.runs/impl_1/vidcap_wrapper.bit $out/vidcap.bit
file copy -force $proj/vidcap.gen/sources_1/bd/vidcap/hw_handoff/vidcap.hwh $out/vidcap.hwh
puts "WROTE $out/vidcap.bit and $out/vidcap.hwh"
puts "TIMING: WNS=$wns WHS=$whs"
