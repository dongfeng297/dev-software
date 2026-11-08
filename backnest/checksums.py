"""Small checksum helpers shared by the feature layer.

CRC32 is implemented here (table driven) so the product path never depends on
zlib.  Tests may compare the result with zlib.crc32 as an independent reference.
"""
from __future__ import annotations

_TABLE = []
for _value in range(256):
    _crc = _value
    for _bit in range(8):
        _crc = (_crc >> 1) ^ (0xEDB88320 if _crc & 1 else 0)
    _TABLE.append(_crc)
_TABLE = tuple(_TABLE)


def crc32(data: bytes, crc: int = 0) -> int:
    """Return the standard CRC-32 (IEEE 802.3) of *data*."""
    value = (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF
    table = _TABLE
    for byte in data:
        value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
    return value ^ 0xFFFFFFFF


class CRC32:
    """Incremental CRC-32 helper."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0xFFFFFFFF

    def update(self, data: bytes) -> None:
        table = _TABLE
        value = self.value
        for byte in data:
            value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
        self.value = value

    def digest(self) -> int:
        return self.value ^ 0xFFFFFFFF
