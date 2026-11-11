# Simulates the Basys 3 harness (fpga/basys3_top.v) with the design's RTL: the PC's UART frames go
# through uart_spi_bridge into the host SPI port. FPGA_SIM drives the 40 MHz clock directly (no
# MMCM) and adds the JB pull-ups. Run: cd fpga/sim && make

SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(CURDIR)/../../src
PROJECT_SOURCES := $(shell sed -n 's/^PROJECT_SOURCES *= *//p' $(CURDIR)/../../test/Makefile)

COMPILE_ARGS    += -DFUNCTIONAL -DFPGA_SIM -I$(SRC_DIR)
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
VERILOG_SOURCES += $(CURDIR)/../basys3_top.v $(CURDIR)/../uart_spi_bridge.v $(CURDIR)/../sram_fpga.v
TOPLEVEL = basys3_top
COCOTB_TEST_MODULES = test_bridge

include $(shell cocotb-config --makefiles)/Makefile.sim
