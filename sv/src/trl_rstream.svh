// trl_rstream.svh — a stream in a database opened for reading.
// Included by trl_pkg.sv.

// A stream in a database opened for reading.
//
// Obtained from :sv:func:`trl_reader::stream_at` or from the
// function :sv:func:`trl_reader::find_stream`. Pass it to a
// cursor's :sv:func:`trl_cursor::create` call to walk only that stream.
class trl_rstream;
    chandle h;       // DPI handle (internal).
    string  m_path;  // Cached stream path (internal); use :sv:func:`path <trl_rstream::path>`.

    // :returns: the full hierarchical stream path
    function string path();
        return m_path;
    endfunction

    // :returns: the stream's instance ID, which matches :sv:prop:`trl_cursor::stream_inst_id`; ``-1`` if invalid
    function int inst_id();
        return (h == null) ? -1 : trl_sv_rstream_inst_id(h);
    endfunction
endclass
