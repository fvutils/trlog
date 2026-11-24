cmake_minimum_required(VERSION 3.14)
project(trlog_vpi C)
set(CMAKE_C_STANDARD 11)

# The IEEE 1800 vpi_user.h is vendored in ext/ (types and constants only;
# the routines are looked up at runtime, see src/trlog_vpi_fn.c), so no
# simulator SDK is needed.  Set VPI_INCLUDE_DIR to use another copy.
set(VPI_INCLUDE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/ext CACHE PATH
    "Directory holding vpi_user.h")
message(STATUS "VPI include dir: ${VPI_INCLUDE_DIR}")

add_library(trlog_vpi SHARED
    src/trlog_vpi_state.c
    src/trlog_vpi.c
    src/trlog_vpi_walk.c
    src/trlog_vpi_value.c
    src/trlog_vpi_fn.c
)
target_include_directories(trlog_vpi
    PUBLIC  include
    PRIVATE src ${VPI_INCLUDE_DIR}
)
target_link_libraries(trlog_vpi PRIVATE trl ${CMAKE_DL_LIBS})
if(WIN32)
    target_link_libraries(trlog_vpi PRIVATE psapi)
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    # Prove the library needs nothing from the simulator at link time.
    target_link_options(trlog_vpi PRIVATE -Wl,--no-undefined)
endif()
set_target_properties(trlog_vpi PROPERTIES
    OUTPUT_NAME  trlog_vpi     # produces libtrlog_vpi.so
    POSITION_INDEPENDENT_CODE ON
    C_VISIBILITY_PRESET hidden  # exports: trlog_vpi.h (TRLOG_VPI_API)
)

install(TARGETS trlog_vpi DESTINATION lib)
install(FILES include/trlog_vpi.h DESTINATION include)

# Unit tests (requires no simulator; uses mock VPI)
if(BUILD_TESTING)
    add_subdirectory(tests)
endif()
