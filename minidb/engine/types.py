"""Column types and binary row (de)serialization.

A row is stored as a compact, schema-ordered byte string. Every column is
encoded as a 1-byte presence flag (0 = NULL) followed by its typed payload:

    INT   -> q   (signed 64-bit)
    FLOAT -> d   (IEEE-754 double)
    TEXT  -> I len + UTF-8 bytes
"""

from __future__ import annotations

import struct
from typing import Any

TYPES = {"INT", "TEXT", "FLOAT"}


class TypeError_(ValueError):
    """Raised when a value cannot be coerced to its column type."""


def coerce(value: Any, col_type: str, col_name: str) -> Any:
    if value is None:
        return None
    if col_type == "INT":
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise TypeError_(f"column {col_name!r} expects INT, got {value!r}")
    if col_type == "FLOAT":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise TypeError_(f"column {col_name!r} expects FLOAT, got {value!r}")
    if col_type == "TEXT":
        if isinstance(value, str):
            return value
        raise TypeError_(f"column {col_name!r} expects TEXT, got {value!r}")
    raise TypeError_(f"unknown type {col_type!r}")


def serialize_row(columns: list[tuple[str, str]], row: dict[str, Any]) -> bytes:
    out = bytearray()
    for name, col_type in columns:
        value = row.get(name)
        if value is None:
            out += b"\x00"
            continue
        out += b"\x01"
        if col_type == "INT":
            out += struct.pack("<q", value)
        elif col_type == "FLOAT":
            out += struct.pack("<d", value)
        else:  # TEXT
            data = value.encode("utf-8")
            out += struct.pack("<I", len(data)) + data
    return bytes(out)


def deserialize_row(columns: list[tuple[str, str]], data: bytes) -> dict[str, Any]:
    row: dict[str, Any] = {}
    pos = 0
    for name, col_type in columns:
        present = data[pos]
        pos += 1
        if not present:
            row[name] = None
            continue
        if col_type == "INT":
            (v,) = struct.unpack_from("<q", data, pos)
            pos += 8
        elif col_type == "FLOAT":
            (v,) = struct.unpack_from("<d", data, pos)
            pos += 8
        else:  # TEXT
            (length,) = struct.unpack_from("<I", data, pos)
            pos += 4
            v = data[pos : pos + length].decode("utf-8")
            pos += length
        row[name] = v
    return row
