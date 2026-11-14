# Vivado non-project build of the Basys 3 harness: synthesis, place and route, reports, bitstream.
#   cd fpga; vivado -mode batch -nojournal -nolog -source build.tcl
# Outputs go to fpga/build/: basys3_top.bit, timing.rpt, utilization.rpt.
# The design's sources are the PROJECT_SOURCES list in test/Makefile (devOps's list), so the FPGA
# build follows the chip; fpga/sram_fpga.v stands in for the IHP SRAM macro.

set here [file normalize [file dirname [info script]]]
set root [file normalize "$here/.."]
set out  "$here/build"
file mkdir $out
cd $out                ;# Vivado writes scratch files (clockInfo.txt) to the working directory

set mk [open "$root/test/Makefile" r]
set sources {}
foreach line [split [read $mk] "\n"] {
    if {[regexp {^PROJECT_SOURCES\s*=\s*(.*)$} $line -> list]} {
        foreach f $list { lappend sources "$root/src/$f" }
    }
}
close $mk
if {[llength $sources] == 0} { error "no PROJECT_SOURCES in test/Makefile" }

read_verilog $sources
read_verilog [list "$here/basys3_top.v" "$here/uart_spi_bridge.v" "$here/sram_fpga.v"]
read_xdc "$here/basys3.xdc"

synth_design -top basys3_top -part xc7a35tcpg236-1 -include_dirs [list "$root/src"]
opt_design
place_design
route_design

report_timing_summary -file "$out/timing.rpt"
report_utilization -file "$out/utilization.rpt"
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts "BUILD RESULT: setup WNS $wns ns, hold WHS $whs ns"
write_bitstream -force "$out/basys3_top.bit"
puts "BUILD DONE: $out/basys3_top.bit"
