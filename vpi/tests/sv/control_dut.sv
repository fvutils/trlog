// control_dut.sv — exercises $trlog_dumpoff / $trlog_dumpon / $trlog_dumpall.

`default_nettype none

module control_dut;
    logic       clk   = 1'b0;
    logic [7:0] count = 8'h00;

    always #5 clk = ~clk;
    always @(posedge clk) count <= count + 1;

    integer i;
    initial begin
        $trlog_dumpfile("control.trl");
        $trlog_dumpvars(0, control_dut);

        $dumpfile("control.vcd");
        $dumpvars(0, control_dut);

        // 100 cycles active.
        for (i = 0; i < 100; i++) begin
            @(posedge clk);
        end
        $trlog_dumpoff;

        // 100 cycles paused.
        for (i = 0; i < 100; i++) begin
            @(posedge clk);
        end
        $trlog_dumpon;
        $trlog_dumpall;

        // 100 more cycles.
        for (i = 0; i < 100; i++) begin
            @(posedge clk);
        end

        $trlog_dumpflush;
        $finish;
    end
endmodule
