// sim_main.cpp — generic Verilator top-level for VPI test DUTs.
//
// Compile with:
//   verilator --vpi --cc <dut>.sv --exe sim_main.cpp \
//             -LDFLAGS "-L<build>/vpi -ltrlog_vpi -L<build>/c -ltrl"
//
// The VPI library is linked statically (via LDFLAGS).  Verilator calls
// vlog_startup_routines[] automatically during simulation init when --vpi
// is used and the library exports that symbol.

#include "verilated.h"

int main(int argc, char **argv) {
    Verilated::commandArgs(argc, argv);

    // Instantiate the DUT (class name is "V<module_name>").
    // We use the generic VerilatedContext API so this file works with any DUT.
    VerilatedContext *ctx = new VerilatedContext;
    ctx->commandArgs(argc, argv);
    ctx->traceEverOn(false);  // TRLOG handles tracing via VPI callbacks.

    // The actual model class is generated at compile time; link against it.
    extern Verilated::VModel *create_model(VerilatedContext *);
    Verilated::VModel *top = create_model(ctx);

    while (!ctx->gotFinish()) {
        ctx->timeInc(1);
        top->eval();
    }

    top->final();
    delete top;
    delete ctx;
    return 0;
}
