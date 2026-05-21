cmake_minimum_required(VERSION 3.14)
project(trlog_vpi C)
set(CMAKE_C_STANDARD 11)

# Locate the VPI header supplied by the simulator SDK.  The bundled
# Verilator header is used as a fallback for header-only compilation.
find_path(VPI_INCLUDE_DIR vpi_user.h
    PATHS
        ${CMAKE_SOURCE_DIR}/packages/verilator/share/verilator/include/vltstd
    NO_DEFAULT_PATH
)
if(NOT VPI_INCLUDE_DIR)
    find_path(VPI_INCLUDE_DIR vpi_user.h)
endif()
if(NOT VPI_INCLUDE_DIR)
    message(FATAL_ERROR "Cannot find vpi_user.h.  "
        "Set VPI_INCLUDE_DIR or ensure the bundled Verilator package is present.")
endif()
message(STATUS "VPI include dir: ${VPI_INCLUDE_DIR}")

add_library(trlog_vpi SHARED
    src/trlog_vpi_state.c
    src/trlog_vpi.c
    src/trlog_vpi_walk.c
    src/trlog_vpi_value.c
)
target_include_directories(trlog_vpi
    PUBLIC  include
    PRIVATE src ${VPI_INCLUDE_DIR}
)
target_link_libraries(trlog_vpi PRIVATE trl)
set_target_properties(trlog_vpi PROPERTIES
    OUTPUT_NAME  trlog_vpi     # produces libtrlog_vpi.so
    POSITION_INDEPENDENT_CODE ON
)

install(TARGETS trlog_vpi DESTINATION lib)
install(FILES include/trlog_vpi.h DESTINATION include)

# Unit tests (requires no simulator; uses mock VPI)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
