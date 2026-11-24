/****************************************************************************
 * bindings.cpp -- embind wrapper over libtrl's read API
 *
 * A thin layer: one Trace object per opened file, holding the heap copy of
 * the bytes that trl_reader_open_mem / trl_rdb_open_mem borrow. Results go
 * back as plain JS objects; 64-bit times and ids are BigInt. The public
 * TypeScript API (ts/) is built on top of this and is where the shape of
 * the API is decided.
 ****************************************************************************/
#include <emscripten/bind.h>
#include <emscripten/val.h>
#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "trl/trl.h"
#include "trl/trl_query.h"

using emscripten::val;

namespace {

val bytes_to_js(const uint8_t *p, size_t n) {
    // Copy out of the WASM heap: a typed_memory_view dies when memory grows.
    val view = val(emscripten::typed_memory_view(n, p));
    return val::global("Uint8Array").new_(view);
}

val attr_to_js(const trl_attr_t &a) {
    val o = val::object();
    o.set("field", a.field_idx);
    o.set("type", (int)a.field_type);
    switch (a.field_type) {
    case TRL_FT_I8: case TRL_FT_I16: case TRL_FT_I32: case TRL_FT_I64:
        o.set("value", val(a.value.i64));
        break;
    case TRL_FT_F32: case TRL_FT_F64:
        o.set("value", a.value.f64);
        break;
    case TRL_FT_STRING:
        o.set("value", std::string(a.value.str ? a.value.str : ""));
        break;
    case TRL_FT_BITVEC: case TRL_FT_BYTES:
        o.set("value", bytes_to_js(a.value.vec.data, a.value.vec.len));
        o.set("width", a.value.vec.width);
        break;
    default:
        o.set("value", val(a.value.u64));
        break;
    }
    return o;
}

class Trace {
public:
    explicit Trace(val bytes) {
        // The C readers borrow the buffer; this copy lives as long as we do.
        buf_ = emscripten::convertJSArrayToNumberVector<uint8_t>(bytes);
        reader_ = trl_reader_open_mem(buf_.data(), buf_.size());
        if (!reader_)
            throw std::runtime_error("not a valid trlog trace");
        // trl_rdb names are a process-wide registry; give each a unique one.
        static unsigned serial = 0;
        std::string name = "wasm" + std::to_string(++serial);
        rdb_ = trl_rdb_open_mem(name.c_str(), buf_.data(), buf_.size());
        if (!rdb_) {
            trl_reader_close(reader_);
            reader_ = nullptr;
            throw std::runtime_error("not a valid trlog trace (transactions)");
        }
    }
    ~Trace() { close(); }

    void close() {
        if (rdb_) { trl_rdb_close(rdb_); rdb_ = nullptr; }
        if (reader_) { trl_reader_close(reader_); reader_ = nullptr; }
        std::vector<uint8_t>().swap(buf_);
    }

    int timescaleExp() {
        check_open();
        return trl_reader_timescale_exp(reader_);
    }

    // Hierarchy walk events in declaration order:
    // [{kind, hierId, name, scopeType, id, typeId, encoding, width, dir, aliasOf}]
    // (kind: trl_hier_event_kind_t). The TS layer builds the scope tree.
    val hierarchy() {
        check_open();
        val out = val::array();
        trl_status_t st = trl_iter_hier(reader_, &Trace::on_hier, &out);
        if (st != TRL_OK)
            throw std::runtime_error("trl_iter_hier failed: " + std::to_string((int)st));
        return out;
    }

    // Changes of one variable with t0 <= time < t1, sorted by time:
    // {times: BigUint64Array, offsets: Uint32Array (n+1), bytes: Uint8Array}.
    // Value i is bytes[offsets[i], offsets[i+1]) in trl_vc_cb's layout.
    val valueChanges(uint32_t var_id, uint64_t t0, uint64_t t1) {
        check_open();
        VcFilter f{var_id, t0, t1, {}};
        trl_status_t st = trl_iter_vc(reader_, &Trace::on_vc, &f);
        if (st != TRL_OK)
            throw std::runtime_error("trl_iter_vc failed: " + std::to_string((int)st));
        std::stable_sort(f.changes.begin(), f.changes.end(),
                         [](const Change &a, const Change &b) { return a.time < b.time; });
        std::vector<uint64_t> times;
        std::vector<uint32_t> offsets{0};
        std::vector<uint8_t> bytes;
        for (const Change &c : f.changes) {
            times.push_back(c.time);
            bytes.insert(bytes.end(), c.value.begin(), c.value.end());
            offsets.push_back((uint32_t)bytes.size());
        }
        val o = val::object();
        o.set("times", val::global("BigUint64Array").new_(
                           val(emscripten::typed_memory_view(times.size(), times.data()))));
        o.set("offsets", val::global("Uint32Array").new_(
                             val(emscripten::typed_memory_view(offsets.size(), offsets.data()))));
        o.set("bytes", bytes_to_js(bytes.data(), bytes.size()));
        return o;
    }

    // [{path, instId}]
    val streams() {
        check_open();
        val out = val::array();
        uint32_t n = trl_rdb_num_streams(rdb_);
        for (uint32_t i = 0; i < n; i++) {
            trl_rstream_t *s = trl_rdb_stream_at(rdb_, i);
            val o = val::object();
            o.set("path", std::string(trl_rstream_path(s)));
            o.set("instId", trl_rstream_inst_id(s));
            out.call<void>("push", o);
        }
        return out;
    }

    // Transactions overlapping [t0, t1) of one stream, or of all when the
    // path is empty.
    val transactions(uint64_t t0, uint64_t t1, std::string stream) {
        check_open();
        val out = val::array();
        trl_rstream_t *rs = nullptr;
        if (!stream.empty()) {
            rs = trl_rdb_stream_find(rdb_, stream.c_str());
            if (!rs) return out;
        }
        trl_cursor_t *c = trl_cursor_new(rdb_, rs, t0, t1);
        if (!c) throw std::runtime_error("trl_cursor_new failed");
        trl_txn_hdr_t h;
        while (trl_cursor_next(c, &h)) {
            val o = val::object();
            o.set("id", val(h.txn_id));
            o.set("streamInstId", h.stream_inst_id);
            o.set("typeId", h.txn_type_id);
            o.set("start", val(h.start));
            o.set("end", val(h.end));
            o.set("parent", val(h.parent));
            val attrs = val::array();
            for (uint32_t i = 0; i < h.attr_count; i++)
                attrs.call<void>("push", attr_to_js(h.attrs[i]));
            o.set("attrs", attrs);
            out.call<void>("push", o);
        }
        trl_cursor_free(c);
        return out;
    }

    // {name, fields: [{name, type}]} or null.
    val schema(uint32_t id) {
        check_open();
        const trl_rschema_t *s = trl_rdb_schema_by_id(rdb_, id);
        if (!s) return val::null();
        val o = val::object();
        o.set("name", std::string(trl_rschema_name(s)));
        val fields = val::array();
        for (uint32_t f = 0; f < trl_rschema_num_fields(s); f++) {
            val fo = val::object();
            fo.set("name", std::string(trl_rschema_field_name(s, f)));
            fo.set("type", (int)trl_rschema_field_type(s, f));
            fields.call<void>("push", fo);
        }
        o.set("fields", fields);
        return o;
    }

private:
    struct Change { uint64_t time; std::vector<uint8_t> value; };
    struct VcFilter { uint32_t var_id; uint64_t t0, t1; std::vector<Change> changes; };

    static void on_vc(uint32_t var_id, uint64_t time, const uint8_t *v, uint32_t len, void *user) {
        VcFilter *f = static_cast<VcFilter *>(user);
        if (var_id != f->var_id || time < f->t0 || time >= f->t1) return;
        f->changes.push_back(Change{time, std::vector<uint8_t>(v, v + len)});
    }
    static void on_hier(const trl_hier_event_t *ev, void *user) {
        val o = val::object();
        o.set("kind", (int)ev->kind);
        o.set("hierId", ev->hier_id);
        o.set("name", std::string(ev->name));
        o.set("scopeType", (int)ev->scope_type);
        o.set("id", ev->id);
        o.set("typeId", ev->type_id);
        o.set("encoding", (int)ev->encoding);
        o.set("width", ev->bit_width);
        o.set("dir", (int)ev->dir);
        o.set("aliasOf", ev->alias_of);
        static_cast<val *>(user)->call<void>("push", o);
    }
    void check_open() const {
        if (!reader_) throw std::runtime_error("trace is closed");
    }

    std::vector<uint8_t> buf_;
    trl_reader_t *reader_ = nullptr;
    trl_rdb_t *rdb_ = nullptr;
};

} // namespace

EMSCRIPTEN_BINDINGS(trlog) {
    emscripten::class_<Trace>("Trace")
        .constructor<val>()
        .function("close", &Trace::close)
        .function("timescaleExp", &Trace::timescaleExp)
        .function("hierarchy", &Trace::hierarchy)
        .function("valueChanges", &Trace::valueChanges)
        .function("streams", &Trace::streams)
        .function("transactions", &Trace::transactions)
        .function("schema", &Trace::schema);
}
