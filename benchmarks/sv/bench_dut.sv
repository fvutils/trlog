// Parameterized benchmark DUT for trace-format storage comparisons.
//
// SIGNAL_MIX selects which signals are active:
//   0 = clk_only  : single 1-bit clock
//   1 = bus_mix   : clock + 8-bit byte bus + 32-bit word bus + 64-bit wide bus
//   2 = dense     : bus_mix + 64-bit packed word toggled every cycle
//
// Steps are controlled at runtime via the "+steps=N" plusarg (default 100000).
//
// Both VCD (--trace-vcd) and FST (--trace-fst) binaries are compiled from this
// file; the only difference is the Verilator trace library linked in.
// Trace output goes to "trace.out" in the working directory.

`default_nettype none

module bench_dut;

    parameter int SIGNAL_MIX = 0;

    // --- signals -------------------------------------------------------
    logic        clk      = 1'b0;
    logic [7:0]  byte_bus = 8'h00;
    logic [31:0] word_bus = 32'h0;
    logic [63:0] wide_bus = 64'h0;
    logic [63:0] dense    = 64'h0;

    // clock: 10 ns period (posedge every 5 ns)
    always #5 clk = ~clk;

    // --- stimulus ------------------------------------------------------
    integer steps     = 100_000;
    integer step_val;
    integer step;

    initial begin : stim
        // Allow runtime override of simulation length
        if ($value$plusargs("steps=%d", step_val))
            steps = step_val;

        $dumpfile("trace.out");
        $dumpvars(0, bench_dut);

        for (step = 0; step < steps; step++) begin
            @(posedge clk);

            if (SIGNAL_MIX >= 1) begin
                byte_bus = step[7:0];
                word_bus = step[31:0];
                wide_bus = {step[31:0], step[31:0]};
            end

            if (SIGNAL_MIX >= 2) begin
                // XOR with a constant to toggle all 64 bits every cycle
                dense = dense ^ 64'hDEAD_BEEF_CAFE_BABE;
            end
        end

        $finish;
    end

endmodule
