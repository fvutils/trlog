cmake_minimum_required(VERSION 3.14)

# Unit test binary: compiled with the mock VPI layer (no simulator required).
# We compile the VPI source files directly so we can access internal state.
#
# The mock is injected via -include so it applies to all compilation units
# in this target.  The VPI_USER_H preprocessor guard then causes the real
# vpi_user.h (included transitively via trlog_vpi_internal.h) to be skipped.

add_executable(test_vpi_unit
    test_vpi_unit.c
    mock_vpi.c
    ../src/trlog_vpi_state.c
    ../src/trlog_vpi_walk.c
)

target_include_directories(test_vpi_unit PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}   # mock_vpi.h resolves first
    ${CMAKE_SOURCE_DIR}/vpi/src   # trlog_vpi_internal.h
)

target_link_libraries(test_vpi_unit PRIVATE trl)

# Force-include the mock header before any other headers in every TU.
# VPI_USER_H skips the real vpi_user.h via its standard include guard.
target_compile_options(test_vpi_unit PRIVATE
    -include ${CMAKE_CURRENT_SOURCE_DIR}/mock_vpi.h
)
target_compile_definitions(test_vpi_unit PRIVATE VPI_USER_H)

add_test(NAME vpi_unit COMMAND test_vpi_unit)
