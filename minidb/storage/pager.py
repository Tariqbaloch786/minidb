"""Physical, checksummed page store.

The database file is a sequence of fixed-size **physical pages** of
``PAGE_SIZE`` bytes. Each physical page is::

    ┌──────────────┬───────────────┬────────────────────────────────┐
    │ crc32 (u32)  │ page_id (u32) │ data (DATA_SIZE bytes)          │
    └──────────────┴───────────────┴────────────────────────────────┘
      offset 0       offset 4        offset 8

Everything above this layer (B+Trees, the catalog, the meta page) works in
terms of the ``DATA_SIZE``-byte *data* area; the 8-byte header is private to the
pager. On every read the pager recomputes the CRC over ``page_id || data`` and
compares it to the stored value, and checks that the stored ``page_id`` matches
the one requested.

What the checksum protects
--------------------------
* single-bit / single-byte flips anywhere in a page's header or data,
* a page written to (or read from) the wrong offset (via the ``page_id`` field),
* a torn or truncated final page (a short read is treated as corruption).

What it does **not** protect
----------------------------
* it is an *integrity* check (CRC32), **not** a cryptographic MAC — it does not
  defend against a deliberate attacker who also rewrites the checksum;
* it does not by itself *repair* corruption — it turns silent corruption into a
  deterministic :class:`CorruptionError`. Recovery from the WAL (re-applying a
  committed after-image) is what can heal a damaged page;
* it says nothing about *logical* consistency (a valid page whose contents are
  semantically wrong); the B+Tree's ``validate()`` covers that separately.

This module does **no caching and no write buffering** — that is the job of
:mod:`minidb.storage.buffer_pool`, which also provides the no-steal buffering
that makes rollback and WAL-ordering correct. A raw ``Pager`` performs immediate
physical I/O and is used directly only by low-level unit tests.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass

PAGE_SIZE = 4096
HEADER_SIZE = 8  # crc32 (u32) + page_id (u32)
DATA_SIZE = PAGE_SIZE - HEADER_SIZE  # bytes available to layers above the pager
MAGIC = b"MDB2"  # bumped from MDB1: the page format now carries checksums

# meta lives in the data area of page 0:
#   magic(4s) page_size(I) num_pages(I) catalog_root(i) free_list_head(i) next_txid(Q)
_META_FMT = "<4sIIiiQ"
_META_SIZE = struct.calcsize(_META_FMT)

NO_PAGE = -1


class CorruptionError(Exception):
    """Raised when a page fails its checksum / identity check, or the file is
    truncated. The engine never continues on corrupted data."""


@dataclass
class Meta:
    page_size: int = PAGE_SIZE
    num_pages: int = 1  # page 0 (meta) always exists
    catalog_root: int = NO_PAGE
    free_list_head: int = NO_PAGE
    next_txid: int = 1

    def pack(self) -> bytes:
        raw = struct.pack(_META_FMT, MAGIC, self.page_size, self.num_pages,
                          self.catalog_root, self.free_list_head, self.next_txid)
        return raw + b"\x00" * (DATA_SIZE - len(raw))

    @classmethod
    def unpack(cls, data: bytes) -> "Meta":
        magic, page_size, num_pages, catalog_root, free_list_head, next_txid = (
            struct.unpack(_META_FMT, data[:_META_SIZE]))
        if magic != MAGIC:
            raise CorruptionError(
                f"not a minidb v2 file (bad magic {magic!r}); "
                "MDB1 files predate page checksums and are not compatible")
        return cls(page_size, num_pages, catalog_root, free_list_head, next_txid)


# -- free-list allocation, shared by the Pager and the BufferPool ----------
def allocate_page_via(meta: Meta, read_page, write_page) -> int:
    """Return a fresh page id, reusing a freed page when possible.

    ``read_page`` / ``write_page`` are supplied by the caller so this logic works
    both for the raw pager (immediate I/O) and the buffer pool (buffered I/O),
    which is important: a page freed inside an uncommitted transaction must be
    reused through the *same* buffered view, not the stale on-disk copy.
    """
    if meta.free_list_head != NO_PAGE:
        page_id = meta.free_list_head
        (next_free,) = struct.unpack("<i", bytes(read_page(page_id)[:4]))
        meta.free_list_head = next_free
        write_page(page_id, b"\x00" * DATA_SIZE)
        return page_id
    page_id = meta.num_pages
    meta.num_pages += 1
    write_page(page_id, b"\x00" * DATA_SIZE)
    return page_id


def free_page_via(meta: Meta, page_id: int, write_page) -> None:
    buf = bytearray(DATA_SIZE)
    struct.pack_into("<i", buf, 0, meta.free_list_head)
    write_page(page_id, buf)
    meta.free_list_head = page_id


class Pager:
    """Immediate, checksummed physical page I/O over a single file."""

    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        if is_new:
            open(path, "wb").close()
        # Unbuffered: the buffer pool is the cache; this avoids stdio read/write
        # interleaving pitfalls and keeps physical I/O explicit.
        self._f = open(path, "r+b", buffering=0)
        if is_new:
            self.meta = Meta()
            self.write_page(0, self.meta.pack())
            self.sync()
        else:
            self.meta = Meta.unpack(self.read_page(0))

    # -- checksummed physical I/O -----------------------------------------
    def read_page(self, page_id: int) -> bytearray:
        if page_id < 0:
            raise CorruptionError(f"invalid page id {page_id}")
        self._f.seek(page_id * PAGE_SIZE)
        raw = self._f.read(PAGE_SIZE)
        if len(raw) < PAGE_SIZE:
            raise CorruptionError(
                f"page {page_id} is truncated ({len(raw)} of {PAGE_SIZE} bytes)")
        stored_crc, stored_id = struct.unpack_from("<II", raw, 0)
        data = raw[HEADER_SIZE:]
        if stored_id != page_id:
            raise CorruptionError(
                f"page {page_id} carries wrong id {stored_id} (misdirected read?)")
        if (zlib.crc32(raw[4:]) & 0xFFFFFFFF) != stored_crc:
            raise CorruptionError(f"page {page_id} failed checksum (corrupted)")
        return bytearray(data)

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        if len(data) != DATA_SIZE:
            raise ValueError(f"page data must be exactly {DATA_SIZE} bytes")
        body = struct.pack("<I", page_id) + bytes(data)  # page_id || data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        page = struct.pack("<I", crc) + body
        self._f.seek(page_id * PAGE_SIZE)
        self._f.write(page)

    # -- allocation --------------------------------------------------------
    def allocate_page(self) -> int:
        return allocate_page_via(self.meta, self.read_page, self.write_page)

    def free_page(self, page_id: int) -> None:
        free_page_via(self.meta, page_id, self.write_page)

    def save_meta(self) -> None:
        self.write_page(0, self.meta.pack())

    # -- durability --------------------------------------------------------
    def sync(self) -> None:
        os.fsync(self._f.fileno())

    def size_pages(self) -> int:
        return os.fstat(self._f.fileno()).st_size // PAGE_SIZE

    def close(self) -> None:
        try:
            self.sync()
        finally:
            self._f.close()
