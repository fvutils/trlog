// top_smoke.sv — Verilator DPI smoke test for the L2 trlog_pkg (phase 4).
//
// Records one tier-2 (open_txn/set_int/meta/close) and one tier-1 (record())
// transaction, closes the database, then reopens it read-only and walks the
// cursor, printing PASS/FAIL markers a test harness greps for. Run with
// +trlog_file=<path> to pick the trace file.

`default_nettype none

import trlog_pkg::*;

module top_smoke;

    initial begin
        trlog_pkg::stream   s;
        trlog_pkg::type_h   t;
        trlog_pkg::txn      x1;
        int f_a, f_b;
        int key_sid, val_sid;
        longint unsigned t1_id, t2_id;
        longint unsigned vals[2];
        int n_ok;
        int n_fail;

        n_ok = 0;
        n_fail = 0;

        if (!trlog_pkg::enabled()) begin
            $display("SMOKE: FAIL recording not enabled (pass +trlog_file=<path>)");
            $finish;
        end

        t = trlog_pkg::type_h::create("pkt");
        f_a = t.add_field("a", trlog_pkg::K_U32);
        f_b = t.add_field("b", trlog_pkg::K_U32);
        t.seal();

        s = trlog_pkg::stream::create("s", null, t);
        if (s == null || !s.en) begin
            $display("SMOKE: FAIL stream create/enabled");
            $finish;
        end

        begin
            trlog_pkg::db d = trlog_pkg::db::get();
            key_sid = d.intern("trlog.origin.component");
            val_sid = d.intern("sv_smoke");
        end

        x1 = s.open_txn(t, 0, 100);
        t1_id = x1.id();
        x1.set_int(f_a, 7);
        x1.set_int(f_b, 8);
        x1.meta(key_sid, val_sid);
        x1.close(150);

        vals[0] = 42;
        vals[1] = 43;
        t2_id = s.record(200, 210, vals, t);

        trlog_pkg::shutdown();

        // --- read it back ---
        begin
            trlog_pkg::reader    r;
            trlog_pkg::cursor    c;
            string path;
            int n;

            void'($value$plusargs("trlog_file=%s", path));
            r = trlog_pkg::reader::open("smoke_r", path);
            if (r == null) begin
                $display("SMOKE: FAIL reader open");
                $finish;
            end

            if (r.num_streams() == 1) n_ok++; else begin n_fail++; $display("SMOKE: FAIL num_streams"); end

            c = trlog_pkg::cursor::create(r, null, 0, 64'hFFFF_FFFF_FFFF_FFFF);
            if (c == null) begin
                $display("SMOKE: FAIL cursor create");
                $finish;
            end

            n = 0;
            while (c.next()) begin
                if (c.id == t1_id) begin
                    if (c.start == 100 && c.end_t == 150 && c.attr_count == 2 &&
                        c.get_int(f_a) == 7 && c.get_int(f_b) == 8 && c.num_metas() == 1) begin
                        n_ok++;
                    end else begin
                        n_fail++;
                        $display("SMOKE: FAIL t1 mismatch start=%0d end=%0d a=%0d b=%0d metas=%0d",
                                  c.start, c.end_t, c.get_int(f_a), c.get_int(f_b), c.num_metas());
                    end
                end else if (c.id == t2_id) begin
                    if (c.start == 200 && c.end_t == 210 && c.get_int(f_a) == 42 && c.get_int(f_b) == 43)
                        n_ok++;
                    else begin
                        n_fail++;
                        $display("SMOKE: FAIL t2 mismatch start=%0d end=%0d a=%0d b=%0d",
                                  c.start, c.end_t, c.get_int(f_a), c.get_int(f_b));
                    end
                end
                n++;
            end
            c.free();

            if (n == 2) n_ok++; else begin n_fail++; $display("SMOKE: FAIL txn count %0d", n); end
        end

        if (n_fail == 0) $display("SMOKE: PASS (%0d checks)", n_ok);
        else $display("SMOKE: FAIL (%0d ok, %0d failed)", n_ok, n_fail);

        $finish;
    end

endmodule : top_smoke
