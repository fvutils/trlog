// smoke_dut.sv — minimal DUT for VPI integration tests.
// Compile with Verilator --bbox-sys --bbox-unsup --sv --vpi.

`default_nettype none

module smoke_dut;
    parameter int CYCLES = 200;

    logic        clk      = 1'b0;
    logic [7:0]  byte_bus = 8'h00;
    logic [31:0] word_bus = 32'h00;
    logic [63:0] wide_bus = 64'h00;

    always #5 clk = ~clk;

    always @(posedge clk) begin
        byte_bus <= byte_bus + 8'd1;
        word_bus <= word_bus + 32'd1;
        wide_bus <= wide_bus + 64'd1;
    end

    integer i;
    initial begin
        $trlog_dumpfile("smoke.trl");
        $trlog_dumpvars(0, smoke_dut);

        $dumpfile("smoke.vcd");
        $dumpvars(0, smoke_dut);

        for (i = 0; i < CYCLES; i++) begin
            @(posedge clk);
        end
        $trlog_dumpflush;
        $finish;
    end
endmodule
