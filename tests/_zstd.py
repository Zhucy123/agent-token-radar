"""Compress fixtures with whatever zstd the machine has.

DSH session logs are zstd-framed, and several tests have to build one. Python
3.14 ships ``compression.zstd``; 3.11-3.13 need the third-party ``zstandard``
package. Either compressor is fine here -- the tests care about the adapter
reading the result, not about which library wrote it. If neither is installed
the test is skipped, not errored: a missing optional decoder is not a failure
of the code under test.
"""

from __future__ import annotations

import unittest


def zstd_compress(data: bytes) -> bytes:
    """Compress ``data`` or skip the calling test if no encoder is available."""
    try:
        from compression import zstd as std_zstd
        return std_zstd.compress(data)
    except ModuleNotFoundError:
        pass
    try:
        import zstandard
        return zstandard.compress(data)
    except ModuleNotFoundError:
        raise unittest.SkipTest(
            "no zstd compressor: use Python 3.14+ or pip install zstandard"
        ) from None
