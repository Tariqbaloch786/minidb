"""Write-ahead log with crash recovery.

This implements a simple, correct *redo* WAL with a WAL-first,
force-at-commit durability policy:

* While a transaction runs, page changes stay in the pager's in-memory cache.
* On commit we append the after-image of every dirty page to the WAL, then a
  ``COMMIT`` record, and ``fsync`` the log. Only then do we flush the pages to
  the main database file.

Recovery therefore gives us atomicity and durability:

* A crash *before* the ``COMMIT`` record is written → the transaction's page
  images are ignored on replay (they were never flushed to the main file
  either). The transaction simply never happened.  (atomicity)
* A crash *after* the ``COMMIT`` record → replay re-applies the logged
  after-images to the main file, so the committed changes survive even if the
  main-file flush never completed.  (durability)

Record format (all little-endian)::

    PAGE   : type(B)=1  txid(Q)  page_id(i)  len(I)  data[len]
    COMMIT : type(B)=2  txid(Q)
    ABORT  : type(B)=3  txid(Q)

Each record is followed by a CRC32 of its own bytes so a torn write at the tail
of the log is detected and truncated rather than misread.
"""

from __future__ import annotations

import os
import struct
import zlib

_PAGE = 1
_COMMIT = 2
_ABORT = 3


class WAL:
    def __init__(self, path: str):
        self.path = path
        # append + read
        if not os.path.exists(path):
            open(path, "wb").close()
        self._f = open(path, "r+b")
        self._f.seek(0, os.SEEK_END)

    # -- writing -----------------------------------------------------------
    def _emit(self, body: bytes) -> None:
        crc = zlib.crc32(body) & 0xFFFFFFFF
        self._f.write(body + struct.pack("<I", crc))

    def log_page(self, txid: int, page_id: int, data: bytes) -> None:
        body = struct.pack("<BQiI", _PAGE, txid, page_id, len(data)) + data
        self._emit(body)

    def log_commit(self, txid: int) -> None:
        self._emit(struct.pack("<BQ", _COMMIT, txid))
        self.sync()

    def log_abort(self, txid: int) -> None:
        self._emit(struct.pack("<BQ", _ABORT, txid))

    def sync(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())

    # -- reading / recovery ------------------------------------------------
    def _records(self):
        """Yield decoded records, stopping cleanly at a torn tail."""
        self._f.seek(0)
        blob = self._f.read()
        pos = 0
        n = len(blob)
        while pos < n:
            if pos + 1 > n:
                break
            rtype = blob[pos]
            try:
                if rtype == _PAGE:
                    if pos + 17 > n:
                        break
                    _, txid, page_id, length = struct.unpack_from("<BQiI", blob, pos)
                    end = pos + 17 + length
                    if end + 4 > n:
                        break
                    data = blob[pos + 17 : end]
                    body = blob[pos:end]
                    (crc,) = struct.unpack_from("<I", blob, end)
                    if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
                        break
                    yield (_PAGE, txid, page_id, data)
                    pos = end + 4
                elif rtype in (_COMMIT, _ABORT):
                    if pos + 9 + 4 > n:
                        break
                    _, txid = struct.unpack_from("<BQ", blob, pos)
                    body = blob[pos : pos + 9]
                    (crc,) = struct.unpack_from("<I", blob, pos + 9)
                    if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
                        break
                    yield (rtype, txid, None, None)
                    pos = pos + 9 + 4
                else:
                    break
            except struct.error:
                break

    def recover(self, pager) -> list[int]:
        """Replay committed transactions onto ``pager``'s file.

        Returns the list of committed txids found (useful for tests).
        """
        committed: set[int] = set()
        aborted: set[int] = set()
        page_writes: list[tuple[int, int, bytes]] = []  # (txid, page_id, data)

        for rtype, txid, page_id, data in self._records():
            if rtype == _PAGE:
                page_writes.append((txid, page_id, data))
            elif rtype == _COMMIT:
                committed.add(txid)
            elif rtype == _ABORT:
                aborted.add(txid)

        if not committed:
            return []

        for txid, page_id, data in page_writes:
            if txid in committed:
                pager.write_page(page_id, data)  # re-stamps a valid checksum
        pager.sync()
        # refresh in-memory meta from the (possibly rewritten) page 0
        from .pager import Meta
        pager.meta = Meta.unpack(pager.read_page(0))
        return sorted(committed)

    def truncate(self) -> None:
        """Drop the log after a checkpoint (all committed data is in the file)."""
        self._f.seek(0)
        self._f.truncate()
        self.sync()

    def close(self) -> None:
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()
