cmake_minimum_required(VERSION 3.14)
project(trlog)

# Installed libraries sit side by side (lib/, or the wheel's trlog/), so
# libtrl_sv and libtrlog_vpi find libtrl next to themselves when a
# simulator loads them without LD_LIBRARY_PATH.
if(APPLE)
    set(CMAKE_INSTALL_RPATH "@loader_path")
elseif(UNIX)
    set(CMAKE_INSTALL_RPATH "$ORIGIN")
endif()

add_subdirectory(c)
add_subdirectory(vpi)

# The SystemVerilog package, for get_svdirs() in an installed wheel.
install(DIRECTORY sv/src/ DESTINATION share/sv)
