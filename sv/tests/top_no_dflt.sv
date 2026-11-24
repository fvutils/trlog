// top_no_dflt.sv — open_txn()/record() with t = null on a stream created
// without a default type (docs_refresh_status.md issue #33).
//
// Each call must print a warning and hand back a no-op transaction (ID 0)
// instead of dereferencing a null dflt_type. Prints "NODFLT: PASS" or
// "NODFLT: FAIL ..." for the test harness. Run with +trlog_file=<path>.

`default_nettype none

import trl_pkg::*;

module top_no_dflt;

    initial begin
        trl_stream s;
        trl_type   t;
        trl_txn    x;
        longint unsigned vals[1];
        int f_a;
        int n_fail;

        n_fail = 0;

        if (!trl_enabled()) begin
            $display("NODFLT: FAIL recording not enabled (pass +trlog_file=<path>)");
            $finish;
        end

        t = trl_type::create("pkt");
        f_a = t.add_field("a", TRL_FT_U32);
        t.seal();

        s = trl_stream::create("nodflt");   // no default type

        x = s.open_txn(null, 0, 10);
        if (x == null || x.id() != 0) begin
            $display("NODFLT: FAIL open_txn(null) id=%0d", (x == null) ? -1 : x.id());
            n_fail++;
        end
        x.set_int(0, 1);   // no-op, must not crash
        x.close(20);

        vals[0] = 5;
        if (s.record(30, 40, vals) != 0) begin
            $display("NODFLT: FAIL record(t=null) returned a transaction ID");
            n_fail++;
        end

        // An explicit type still records.
        x = s.open_txn(t, 0, 50);
        if (x.id() == 0) begin
            $display("NODFLT: FAIL open_txn(t) id=0");
            n_fail++;
        end
        x.set_int(f_a, 9);
        x.close(60);

        trl_db::close_all();
        if (n_fail == 0) $display("NODFLT: PASS");
        $finish;
    end

endmodule
