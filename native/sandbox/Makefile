BUILD_FILE := $(lastword $(MAKEFILE_LIST))
PROJECT_DIR := $(abspath $(dir $(BUILD_FILE)))
BUILD_DIR ?= $(PROJECT_DIR)/build

CLANG ?= clang
CLANG_FORMAT ?= clang-format
CLANG_TIDY ?= clang-tidy
PKG_CONFIG ?= pkg-config

SANDBOX := $(BUILD_DIR)/neu-box-sandbox
BPF_OBJECT := $(BUILD_DIR)/device_block.o
BPF_DEPENDENCY := $(BUILD_DIR)/device_block.d
RESERVATION_TEST := $(BUILD_DIR)/neu-box-sandbox-reservation-test
OBJECT_DIR := $(BUILD_DIR)/obj
RESERVATION_TEST_SOURCES := $(PROJECT_DIR)/tests/reservation_test.cpp \
	$(PROJECT_DIR)/src/reservation.cpp

SANDBOX_SOURCE_NAMES := bpf cgroup main reservation state
SANDBOX_OBJECTS := $(addprefix $(OBJECT_DIR)/,$(addsuffix .o,$(SANDBOX_SOURCE_NAMES)))
SANDBOX_DEPENDENCIES := $(SANDBOX_OBJECTS:.o=.d)
NATIVE_CPP_SOURCES := $(addprefix $(PROJECT_DIR)/src/,$(addsuffix .cpp,$(SANDBOX_SOURCE_NAMES))) \
	$(PROJECT_DIR)/tests/reservation_test.cpp
NATIVE_FORMAT_SOURCES := $(NATIVE_CPP_SOURCES) \
	$(PROJECT_DIR)/bpf/device_block.bpf.c

REQUIRED_CPPFLAGS := -D_GNU_SOURCE -D_FORTIFY_SOURCE=2 -I$(PROJECT_DIR)/src
REQUIRED_CXXFLAGS := -std=c++17 -O3 -DNDEBUG -fPIE \
	-fstack-protector-strong -Wall -Wextra -Wformat=2 -Wshadow -Wconversion
REQUIRED_LDFLAGS := -pie -Wl,-z,relro -Wl,-z,now -Wl,-z,noexecstack
LIBBPF_CFLAGS := $(shell $(PKG_CONFIG) --cflags libbpf 2>/dev/null)
LIBBPF_LIBS := $(shell $(PKG_CONFIG) --libs libbpf 2>/dev/null)

.PHONY: all check-dependencies test format lint clean

all: $(SANDBOX)

check-dependencies:
	@$(PKG_CONFIG) --atleast-version=1.0 libbpf 2>/dev/null || { \
		echo "libbpf >= 1.0 is required (pkg-config module: libbpf)" >&2; \
		exit 1; \
	}

$(BUILD_DIR) $(OBJECT_DIR):
	mkdir -p $@

$(OBJECT_DIR)/%.o: $(PROJECT_DIR)/src/%.cpp $(PROJECT_DIR)/src/sandbox.hpp $(BUILD_FILE) | $(OBJECT_DIR) check-dependencies
	$(CXX) $(CPPFLAGS) $(REQUIRED_CPPFLAGS) $(LIBBPF_CFLAGS) \
		$(CXXFLAGS) $(REQUIRED_CXXFLAGS) -MMD -MP -c $< -o $@

$(BPF_OBJECT): $(PROJECT_DIR)/bpf/device_block.bpf.c $(BUILD_FILE) | $(BUILD_DIR) check-dependencies
	$(CLANG) $(LIBBPF_CFLAGS) -O2 -g -target bpf -MD -MP \
		-MF $(BPF_DEPENDENCY) -c $< -o $@

$(SANDBOX): $(SANDBOX_OBJECTS) $(BPF_OBJECT) $(BUILD_FILE)
	$(CXX) $(SANDBOX_OBJECTS) $(LDFLAGS) $(REQUIRED_LDFLAGS) \
		-o $@ $(LIBBPF_LIBS) $(LDLIBS)

$(RESERVATION_TEST): $(RESERVATION_TEST_SOURCES) \
		$(PROJECT_DIR)/src/sandbox.hpp $(BUILD_FILE) | $(BUILD_DIR)
	$(CXX) $(CPPFLAGS) $(REQUIRED_CPPFLAGS) $(CXXFLAGS) \
		$(REQUIRED_CXXFLAGS) $(RESERVATION_TEST_SOURCES) \
		$(LDFLAGS) $(REQUIRED_LDFLAGS) -o $@

test: $(RESERVATION_TEST)
	$(RESERVATION_TEST)

format:
	@command -v $(CLANG_FORMAT) >/dev/null || { \
		echo "clang-format is required for native formatting" >&2; exit 127; \
	}
	$(CLANG_FORMAT) -i $(NATIVE_FORMAT_SOURCES)

lint:
	@command -v $(CLANG_TIDY) >/dev/null || { \
		echo "clang-tidy is required for native linting" >&2; exit 127; \
	}
	$(CLANG_TIDY) $(NATIVE_CPP_SOURCES) -- \
		$(CPPFLAGS) $(REQUIRED_CPPFLAGS) $(LIBBPF_CFLAGS) \
		$(CXXFLAGS) $(REQUIRED_CXXFLAGS)

clean:
	$(RM) $(SANDBOX) $(BPF_OBJECT) $(RESERVATION_TEST) \
		$(SANDBOX_OBJECTS) $(SANDBOX_DEPENDENCIES) $(BPF_DEPENDENCY)
	@rmdir $(OBJECT_DIR) $(BUILD_DIR) 2>/dev/null || true

-include $(SANDBOX_DEPENDENCIES) $(BPF_DEPENDENCY)
