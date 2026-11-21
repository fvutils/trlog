// trlog_final.sv — shuts recording down at end of simulation (§5.2, option 2).
// Instantiate (or bind) this once at the top level. A user who calls
// trlog_pkg::shutdown() explicitly (e.g. from uvm_component::final_phase)
// makes this redundant but harmless: trl_db_close on an already-closed
// database is a no-op in trl_rec.h.

module trlog_final;
    final begin
        trlog_pkg::shutdown();
    end
endmodule : trlog_final
