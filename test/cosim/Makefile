# Co-simulation: the model against the chip (README.md). RTL by default; GATES=yes runs the
# gate-level netlist (pins and registers only: synthesis removes the trace port).

SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../../src

# devOps's RTL file list and SRAM models (test/Makefile), so co-sim builds what the RTL tests build
PROJECT_SOURCES := $(shell sed -n 's/^PROJECT_SOURCES *= *//p' $(PWD)/../Makefile)
SRAM_MODELS = $(PWD)/../sram/RM_IHPSG13_1P_512x32_c2_bm_bist.v $(PWD)/../sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v

ifneq ($(GATES),yes)

SIM_BUILD        = sim_build/rtl
COMPILE_ARGS    += -DFUNCTIONAL
VERILOG_SOURCES += $(SRAM_MODELS)
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
export COSIM_GATES = 0

else

# as test/Makefile; the GDS action copies the netlist to test/gate_level_netlist.v
GL_NETLIST      ?= $(PWD)/../gate_level_netlist.v
SIM_BUILD        = sim_build/gl
COMPILE_ARGS    += -DGL_TEST -DFUNCTIONAL -DSIM
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_udp.v
VERILOG_SOURCES += $(PDK_ROOT)/ihp-sg13cmos5l/libs.ref/sg13cmos5l_stdcell/verilog/sg13cmos5l_stdcell.v
VERILOG_SOURCES += $(SRAM_MODELS)
VERILOG_SOURCES += $(GL_NETLIST)
export COSIM_GATES = 1

endif

ifeq ($(strip $(PROJECT_SOURCES)),)
$(error no PROJECT_SOURCES in ../Makefile)
endif

COMPILE_ARGS    += -I$(SRC_DIR)
VERILOG_SOURCES += $(PWD)/../tb.v
TOPLEVEL = tb

COCOTB_TEST_MODULES = test_cosim

include $(shell cocotb-config --makefiles)/Makefile.sim
