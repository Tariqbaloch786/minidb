"""Paged, file-backed storage.

The database lives in a single file that is divided into fixed-size *pages*.
Everything above this layer (B+Trees, the catalog, rows) is expressed in terms
of page reads and writes, exactly like a real RDBMS.

Page 0 is the *meta page*: a small header describing the rest of the file
(next transaction id, catalog location, free-list head, ...).

The pager keeps a small in-memory cache of dirty pages so that a transaction
can buffer its writes and only push them to the WAL / main file at commit time
(a WAL-first, force-at-commit policy). See :mod:`minidb.storage.wal`.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

PAGE_SIZE = 4096
MAGIC = b"MDB1"

# struct layout of the meta page (page 0), little-endian:
#   magic(4s) page_size(I) num_pages(I) catalog_root(i)
#   free_list_head(i) next_txid(Q)
_META_FMT = "<4sIIiiQ"
_META_SIZE = struct.calcsize(_META_FMT)

NO_PAGE = -1


@dataclass
class Meta:
    page_size: int = PAGE_SIZE
    num_pages: int = 1  # page 0 (meta) always exists
    catalog_root: int = NO_PAGE
    free_list_head: int = NO_PAGE
    next_txid: int = 1

    def pack(self) -> bytes:
        raw = struct.pack(
            _META_FMT,
            MAGIC,
            self.page_size,
            self.num_pages,
            self.catalog_root,
            self.free_list_head,
            self.next_txid,
        )
        return raw + b"\x00" * (PAGE_SIZE - len(raw))

    @classmethod
    def unpack(cls, data: bytes) -> "Meta":
        magic, page_size, num_pages, catalog_root, free_list_head, next_txid = (
            struct.unpack(_META_FMT, data[:_META_SIZE])
        )
        if magic != MAGIC:
            raise ValueError("not a minidb file (bad magic)")
        return cls(page_size, num_pages, catalog_root, free_list_head, next_txid)


class Pager:
    """Reads and writes fixed-size pages to a single file.

    Writes are buffered in ``self._cache`` until :meth:`flush` is called, which
    lets a transaction stage many page changes and commit them atomically via
    the WAL layer.
    """

    def __init__(self, path: str):
        self.path = path
        is_new = not os.path.exists(path) or os.path.getsize(path) == 0
        # ``r+b`` cannot create the file, so create it first if needed.
        if is_new:
            open(path, "wb").close()
        self._f = open(path, "r+b")
        self._cache: dict[int, bytearray] = {}
        self._dirty: set[int] = set()  # pages actually modified since last flush

        if is_new:
            self.meta = Meta()
            self._write_raw(0, self.meta.pack())
            self._f.flush()
        else:
            self.meta = Meta.unpack(self._read_raw(0))

    # -- raw, un-cached IO -------------------------------------------------
    def _read_raw(self, page_id: int) -> bytes:
        self._f.seek(page_id * PAGE_SIZE)
        data = self._f.read(PAGE_SIZE)
        if len(data) < PAGE_SIZE:
            data = data + b"\x00" * (PAGE_SIZE - len(data))
        return data

    def _write_raw(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise ValueError(f"page must be exactly {PAGE_SIZE} bytes")
        self._f.seek(page_id * PAGE_SIZE)
        self._f.write(data)

    # -- cached page access ------------------------------------------------
    def read_page(self, page_id: int) -> bytearray:
        if page_id in self._cache:
            return self._cache[page_id]
        buf = bytearray(self._read_raw(page_id))
        self._cache[page_id] = buf
        return buf

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        if len(data) != PAGE_SIZE:
            raise ValueError(f"page must be exactly {PAGE_SIZE} bytes")
        self._cache[page_id] = bytearray(data)
        self._dirty.add(page_id)

    # -- allocation --------------------------------------------------------
    def allocate_page(self) -> int:
        """Return a fresh page id, reusing a freed page when possible."""
        if self.meta.free_list_head != NO_PAGE:
            page_id = self.meta.free_list_head
            # first 4 bytes of a free page point at the next free page
            (next_free,) = struct.unpack("<i", bytes(self.read_page(page_id)[:4]))
            self.meta.free_list_head = next_free
            self.write_page(page_id, b"\x00" * PAGE_SIZE)
            return page_id
        page_id = self.meta.num_pages
        self.meta.num_pages += 1
        self.write_page(page_id, b"\x00" * PAGE_SIZE)
        return page_id

    def free_page(self, page_id: int) -> None:
        buf = bytearray(PAGE_SIZE)
        struct.pack_into("<i", buf, 0, self.meta.free_list_head)
        self.write_page(page_id, buf)
        self.meta.free_list_head = page_id

    # -- persistence -------------------------------------------------------
    def has_writes(self) -> bool:
        return bool(self._dirty)

    def dirty_pages(self) -> dict[int, bytes]:
        """Modified pages plus the refreshed meta page (page 0)."""
        self._cache[0] = bytearray(self.meta.pack())
        ids = self._dirty | {0}
        return {pid: bytes(self._cache[pid]) for pid in sorted(ids)}

    def flush(self) -> None:
        """Write modified pages (and meta) to the main file and fsync."""
        for page_id, buf in self.dirty_pages().items():
            self._write_raw(page_id, buf)
        self._f.flush()
        os.fsync(self._f.fileno())
        self._dirty.clear()
        self._cache.clear()

    def discard(self) -> None:
        """Drop buffered writes (used on rollback), keeping meta consistent."""
        self._cache.clear()
        self._dirty.clear()
        self.meta = Meta.unpack(self._read_raw(0))

    def apply_raw(self, page_id: int, data: bytes) -> None:
        """Write an after-image straight to disk during WAL recovery."""
        self._write_raw(page_id, data)
        if page_id == 0:
            self.meta = Meta.unpack(data)

    def close(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
