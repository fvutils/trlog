// Closes all recording databases from a ``final`` block at the end of
// simulation. Instantiate it once at the top level::
//
//     trl_final u_trl_final();
//
// Redundant, but harmless, if the test calls ``trl_shutdown()`` itself.
module trl_final;
    // sv_api_design.md §5.2, option 2. A second close is a no-op:
    // trl_db_close on an already-closed database does nothing in trl_rec.h.
    final begin
        trl_pkg::trl_shutdown();
    end
endmodule : trl_final
