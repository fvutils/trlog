// deep_hier_dut.sv — 3-level hierarchy DUT.

`default_nettype none

module level2 (
    input  logic clk,
    output logic [7:0] out_a,
    output logic [7:0] out_b
);
    logic [7:0] counter_a = 8'h00;
    logic [7:0] counter_b = 8'hFF;
    always @(posedge clk) begin
        counter_a <= counter_a + 1;
        counter_b <= counter_b - 1;
    end
    assign out_a = counter_a;
    assign out_b = counter_b;
endmodule

module level1 (
    input  logic clk,
    output logic [7:0] sum
);
    logic [7:0] a, b;
    level2 l2 (.clk(clk), .out_a(a), .out_b(b));
    assign sum = a + b;
endmodule

module deep_hier_dut;
    parameter int CYCLES = 100;

    logic       clk = 1'b0;
    logic [7:0] top_sig;

    always #5 clk = ~clk;

    level1 l1 (.clk(clk), .sum(top_sig));

    integer i;
    initial begin
        $trlog_dumpfile("deep_hier.trl");
        $trlog_dumpvars(0, deep_hier_dut);

        $dumpfile("deep_hier.vcd");
        $dumpvars(0, deep_hier_dut);

        for (i = 0; i < CYCLES; i++) begin
            @(posedge clk);
        end
        $finish;
    end
endmodule
