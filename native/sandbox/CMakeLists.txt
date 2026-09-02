cmake_minimum_required(VERSION 3.20)

project(neu_box_sandbox LANGUAGES CXX)

include(CTest)
include(GNUInstallDirs)
include(CheckCXXCompilerFlag)
include(CheckLinkerFlag)
include(CheckPIESupported)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(
    NEU_BOX_SANDBOX_STATIC_LIBBPF
    "Link libbpf into neu-box-sandbox instead of requiring libbpf.so at runtime"
    ON
)

find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBBPF REQUIRED IMPORTED_TARGET libbpf>=1.0)
find_program(CLANG_EXECUTABLE NAMES clang REQUIRED)

set(BPF_SOURCE ${CMAKE_CURRENT_SOURCE_DIR}/bpf/device_block.bpf.c)
set(BPF_OBJECT ${CMAKE_CURRENT_BINARY_DIR}/device_block.o)
set(BPF_INCLUDE_FLAGS ${LIBBPF_INCLUDE_DIRS})
list(TRANSFORM BPF_INCLUDE_FLAGS PREPEND "-I")

add_custom_command(
    OUTPUT ${BPF_OBJECT}
    COMMAND
        ${CLANG_EXECUTABLE} ${BPF_INCLUDE_FLAGS}
        -O2 -g -target bpf -c ${BPF_SOURCE}
        -o ${BPF_OBJECT}
    DEPENDS ${BPF_SOURCE}
    COMMENT "Building device_block.o"
    COMMAND_EXPAND_LISTS
    VERBATIM
)
add_custom_target(device_block_bpf ALL DEPENDS ${BPF_OBJECT})

add_executable(neu-box-sandbox
    src/bpf.cpp
    src/cgroup.cpp
    src/main.cpp
    src/reservation.cpp
    src/state.cpp
)
add_dependencies(neu-box-sandbox device_block_bpf)

check_pie_supported(LANGUAGES CXX OUTPUT_VARIABLE PIE_ERROR)
if(NOT CMAKE_CXX_LINK_PIE_SUPPORTED)
    message(FATAL_ERROR "C++ toolchain does not support PIE: ${PIE_ERROR}")
endif()
set_property(TARGET neu-box-sandbox PROPERTY POSITION_INDEPENDENT_CODE ON)

check_cxx_compiler_flag(
    "-fstack-protector-strong"
    NEU_BOX_HAS_STACK_PROTECTOR_STRONG
)
if(NOT NEU_BOX_HAS_STACK_PROTECTOR_STRONG)
    message(FATAL_ERROR "C++ toolchain lacks -fstack-protector-strong")
endif()
target_compile_options(neu-box-sandbox PRIVATE -fstack-protector-strong)
target_compile_definitions(
    neu-box-sandbox
    PRIVATE $<$<NOT:$<CONFIG:Debug>>:_FORTIFY_SOURCE=2>
)

foreach(
    hardening_link_flag
    IN ITEMS "-Wl,-z,relro" "-Wl,-z,now" "-Wl,-z,noexecstack"
)
    string(MAKE_C_IDENTIFIER
        "NEU_BOX_HAS_${hardening_link_flag}"
        hardening_link_flag_result
    )
    check_linker_flag(CXX "${hardening_link_flag}" ${hardening_link_flag_result})
    if(NOT ${hardening_link_flag_result})
        message(FATAL_ERROR
            "C++ linker lacks required hardening flag ${hardening_link_flag}"
        )
    endif()
    target_link_options(neu-box-sandbox PRIVATE "${hardening_link_flag}")
endforeach()

target_compile_definitions(neu-box-sandbox PRIVATE _GNU_SOURCE)
target_compile_options(neu-box-sandbox PRIVATE
    -Wall
    -Wextra
    -Wformat=2
    -Wshadow
    -Wconversion
)
target_include_directories(neu-box-sandbox PRIVATE src)

if(NEU_BOX_SANDBOX_STATIC_LIBBPF)
    find_file(
        LIBBPF_STATIC_ARCHIVE
        NAMES libbpf.a
        HINTS ${LIBBPF_STATIC_LIBRARY_DIRS} ${LIBBPF_LIBRARY_DIRS}
        PATHS /usr/lib /usr/lib64 /usr/local/lib
              /usr/lib/${CMAKE_LIBRARY_ARCHITECTURE}
        PATH_SUFFIXES lib lib64
        REQUIRED
    )

    # Keep only libbpf itself out of the target-node runtime contract. Its
    # private dependencies remain normal shared-library dependencies managed
    # by RPM. LIBBPF_STATIC_LIBRARIES comes from `pkg-config --static` and has
    # libbpf first, followed by the libraries needed to resolve libbpf.a.
    set(LIBBPF_RUNTIME_DEPENDENCIES ${LIBBPF_STATIC_LIBRARIES})
    list(REMOVE_ITEM LIBBPF_RUNTIME_DEPENDENCIES bpf)
    if(NOT LIBBPF_RUNTIME_DEPENDENCIES)
        message(FATAL_ERROR
            "libbpf pkg-config metadata has no private link dependencies"
        )
    endif()

    target_include_directories(neu-box-sandbox PRIVATE ${LIBBPF_INCLUDE_DIRS})
    target_link_directories(
        neu-box-sandbox PRIVATE ${LIBBPF_STATIC_LIBRARY_DIRS}
    )
    target_link_options(
        neu-box-sandbox PRIVATE ${LIBBPF_STATIC_LDFLAGS_OTHER}
    )
    target_link_libraries(neu-box-sandbox PRIVATE
        ${LIBBPF_STATIC_ARCHIVE}
        ${LIBBPF_RUNTIME_DEPENDENCIES}
        ${CMAKE_DL_LIBS}
    )
else()
    target_link_libraries(neu-box-sandbox PRIVATE PkgConfig::LIBBPF)
endif()

if(BUILD_TESTING)
    add_executable(neu-box-sandbox-reservation-test
        tests/reservation_test.cpp
        src/reservation.cpp
    )
    target_include_directories(neu-box-sandbox-reservation-test PRIVATE src)
    target_compile_options(neu-box-sandbox-reservation-test PRIVATE
        -Wall
        -Wextra
        -Wformat=2
        -Wshadow
        -Wconversion
    )
    add_test(
        NAME neu-box-sandbox-reservation
        COMMAND neu-box-sandbox-reservation-test
    )
endif()

set(NEU_BOX_SANDBOX_INSTALL_DIR ${CMAKE_INSTALL_LIBEXECDIR}/neu-box)
install(
    TARGETS neu-box-sandbox
    RUNTIME DESTINATION ${NEU_BOX_SANDBOX_INSTALL_DIR}
)
install(
    FILES ${BPF_OBJECT}
    DESTINATION ${NEU_BOX_SANDBOX_INSTALL_DIR}
)
