"""Secondary indexes.

A secondary index is a second B+Tree, keyed on an **order-preserving encoding**
of the indexed column value(s) (see :func:`encode_value`) with the row's primary
key appended so duplicate values stay distinct and ordered. The stored value is
the primary key, so an index lookup yields candidate PKs which the executor then
resolves against the primary tree under MVCC visibility.

Encoding is *memcomparable*: lexicographic ``bytes`` comparison of two encoded
keys matches the SQL ordering of the underlying values. That is what lets one
plain byte-keyed B+Tree serve equality lookups **and** range scans.

Index maintenance is insert-only (entries are added on INSERT and on the new
value of an UPDATE, never removed on DELETE/UPDATE). Reads treat index hits as
*candidates* and re-check the actually-visible row, so stale entries are
harmless; a future ``VACUUM`` reclaims them. This mirrors how PostgreSQL keeps
index entries and cleans them lazily.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Optional

from ..storage.pager import NO_PAGE

_SIGN = 1 << 63
_U64 = 0xFFFFFFFFFFFFFFFF


@dataclass
class IndexSchema:
    name: str
    table: str
    columns: list[str]
    unique: bool = False
    root_page_id: int = NO_PAGE


# -- order-preserving value encoding ---------------------------------------
def _enc_int(value: int) -> bytes:
    # map signed range to unsigned so big-endian bytes sort like the ints do
    return struct.pack(">Q", (value + _SIGN) & _U64)


def _enc_float(value: float) -> bytes:
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    bits = (bits ^ _U64) if (bits & _SIGN) else (bits | _SIGN)
    return struct.pack(">Q", bits)


def _enc_text(value: str) -> bytes:
    # escape NUL so the 0x00 0x00 terminator is unambiguous; keeps byte order
    return value.encode("utf-8").replace(b"\x00", b"\x00\xff") + b"\x00\x00"


def encode_value(col_type: str, value: Any) -> bytes:
    """Encode one column value to order-preserving bytes.

    A leading tag byte keeps NULL (``0x00``) sorting before every non-NULL
    value (``0x01`` + payload).
    """
    if value is None:
        return b"\x00"
    if col_type == "INT":
        return b"\x01" + _enc_int(int(value))
    if col_type == "FLOAT":
        return b"\x01" + _enc_float(float(value))
    if col_type == "TEXT":
        return b"\x01" + _enc_text(value)
    raise ValueError(f"cannot index column of type {col_type!r}")


def encode_prefix(col_types: list[str], values: list[Any]) -> bytes:
    """Encode the indexed column values (no primary-key suffix)."""
    return b"".join(encode_value(t, v) for t, v in zip(col_types, values))


def encode_key(col_types: list[str], values: list[Any], pk: int) -> bytes:
    """Full index key: the encoded values followed by the order-preserving PK."""
    return encode_prefix(col_types, values) + struct.pack(">Q", (pk + _SIGN) & _U64)


def prefix_upper(prefix: bytes) -> Optional[bytes]:
    """Smallest byte string strictly greater than every key starting with
    ``prefix`` — the exclusive upper bound of a prefix scan. ``None`` means the
    prefix is unbounded above (all ``0xff``)."""
    b = bytearray(prefix)
    while b and b[-1] == 0xFF:
        b.pop()
    if not b:
        return None
    b[-1] += 1
    return bytes(b)


def pack_pk(pk: int) -> bytes:
    return struct.pack("<q", pk)


def unpack_pk(data: bytes) -> int:
    return struct.unpack("<q", data)[0]
