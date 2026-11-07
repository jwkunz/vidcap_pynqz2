# ps7_preset.tcl - Apply the official PYNQ-Z2 Zynq PS configuration to a processing_system7 cell.
#
# The PS settings (DDR3 timing, MIO, SD, Ethernet, UART, ...) are board-specific and easy to get
# subtly wrong, so they are taken verbatim from PYNQ's own base design (pinned tag, fetched by
# fetch_refs.sh into workspace/vivado/refs) instead of being copied into this repository.
proc apply_pynqz2_ps7 {cell base_tcl} {
    set fh [open $base_tcl r]
    set txt [read $fh]
    close $fh
    set a [string first "create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 ps7_0" $txt]
    if {$a < 0} { error "ps7_0 block not found in $base_tcl" }
    set b [string first "set_property -dict \[list" $txt $a]
    set c [string first "\] \$ps7_0" $txt $b]
    if {$b < 0 || $c < 0} { error "could not delimit the ps7_0 properties in $base_tcl" }
    set body [string range $txt $b [expr {$c}]]      ;# "set_property -dict [list ... ]"
    eval "$body \[get_bd_cells $cell\]"
}
