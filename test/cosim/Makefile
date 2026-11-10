# Co-simulation: the model against the RTL (README.md). RTL only: synthesis removes the trace port.

SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(PWD)/../../src

SIM_BUILD        = sim_build/rtl
COMPILE_ARGS    += -DFUNCTIONAL -I$(SRC_DIR)
VERILOG_SOURCES += $(PWD)/../sram/RM_IHPSG13_1P_512x32_c2_bm_bist.v $(PWD)/../sram/RM_IHPSG13_1P_core_behavioral_bm_bist.v
VERILOG_SOURCES += $(wildcard $(SRC_DIR)/*.v)
VERILOG_SOURCES += $(PWD)/../tb.v
TOPLEVEL = tb

COCOTB_TEST_MODULES = test_cosim

include $(shell cocotb-config --makefiles)/Makefile.sim
