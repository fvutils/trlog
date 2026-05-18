"""TRLOG exception hierarchy."""


class ZstError(Exception):
    """Base class for all TRLOG errors."""


class ZstMagicError(ZstError):
    """File magic bytes do not match the TRLOG specification."""


class ZstVersionError(ZstError):
    """Unsupported file version."""


class ZstFormatError(ZstError):
    """Malformed or inconsistent data in the TRLOG file."""


class ZstCompressionError(ZstError):
    """Unknown or unavailable compression algorithm."""


class ZstCompressionUnavailable(ZstCompressionError):
    """Required compression library (e.g. zstandard) is not installed."""


class ZstNoIndexError(ZstError):
    """Operation requires a seek index, but none is present in the file."""


class ZstCorruptError(ZstError):
    """File data is corrupt (e.g. bad index_offset, checksum failure)."""
