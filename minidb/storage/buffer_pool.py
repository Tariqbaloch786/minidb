"""A bounded buffer pool between the storage engine and the physical pager.

    engine (B+Tree / catalog)
        │  read_page / write_page / allocate_page / free_page
        ▼
    BufferPool   ← bounded LRU cache, pin/unpin, dirty tracking, stats
        │  physical read/write
        ▼
    Pager        ← checksummed page I/O
        ▼
    disk

Durability (no-steal, force-at-commit)
--------------------------------------
The pool **never writes a dirty page to disk on its own**. Dirty pages hold the
uncommitted changes of the active transaction and stay resident until the
transaction commits, at which point :class:`~minidb.database.Database` logs their
after-images to the WAL, fsyncs the commit, and only then calls :meth:`flush`.

This is a *no-steal* policy, and it is exactly what preserves the WAL rule:

    the WAL is made durable BEFORE the dirty page reaches the main file.

Because uncommitted changes never reach disk, ``ROLLBACK`` is just "drop the
dirty frames", and a crash simply leaves the last committed state on disk (the
WAL redoes anything whose commit record made it out).

Consequences
------------
* Eviction only ever removes **clean, unpinned** pages (dropping them is safe —
  they are identical to what is on disk).
* If every resident frame is dirty or pinned, the pool cannot shrink; rather
  than lose data or deadlock it temporarily grows past ``capacity`` (reported in
  :meth:`stats`). So a single transaction's dirty working set must fit in memory
  — a documented limitation of the no-steal design (no UNDO logging yet).
"""

from __future__ import annotations

from collections import OrderedDict

from .pager import DATA_SIZE, Meta, allocate_page_via, free_page_via

DEFAULT_CAPACITY = 1024  # pages (~4 MiB of page data at 4 KiB pages)


class _Frame:
    __slots__ = ("data", "dirty", "pins")

    def __init__(self, data: bytearray, dirty: bool = False, pins: int = 0):
        self.data = data
        self.dirty = dirty
        self.pins = pins


class BufferPool:
    def __init__(self, pager, capacity: int = DEFAULT_CAPACITY):
        self._pager = pager
        self.capacity = max(4, capacity)
        self._frames: "OrderedDict[int, _Frame]" = OrderedDict()  # LRU: front = oldest
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def meta(self) -> Meta:
        return self._pager.meta

    # -- page access -------------------------------------------------------
    def read_page(self, page_id: int) -> bytearray:
        frame = self._frames.get(page_id)
        if frame is not None:
            self.hits += 1
            self._frames.move_to_end(page_id)
            return frame.data
        self.misses += 1
        data = self._pager.read_page(page_id)  # physical + checksum verified
        self._install(page_id, data, dirty=False)
        return data

    def write_page(self, page_id: int, data: bytes | bytearray) -> None:
        if len(data) != DATA_SIZE:
            raise ValueError(f"page data must be exactly {DATA_SIZE} bytes")
        frame = self._frames.get(page_id)
        if frame is not None:
            frame.data = bytearray(data)
            frame.dirty = True
            self._frames.move_to_end(page_id)
        else:
            self._install(page_id, bytearray(data), dirty=True)

    def _install(self, page_id: int, data: bytes | bytearray, dirty: bool) -> None:
        self._make_room()
        self._frames[page_id] = _Frame(bytearray(data), dirty, 0)
        self._frames.move_to_end(page_id)

    def _make_room(self) -> None:
        while len(self._frames) >= self.capacity:
            victim = self._victim()
            if victim is None:
                return  # all frames dirty/pinned -> grow rather than lose data
            del self._frames[victim]
            self.evictions += 1

    def _victim(self):
        for page_id, frame in self._frames.items():  # oldest first (LRU)
            if not frame.dirty and frame.pins == 0:
                return page_id
        return None

    # -- pinning -----------------------------------------------------------
    def pin(self, page_id: int) -> None:
        if page_id not in self._frames:
            self.read_page(page_id)
        self._frames[page_id].pins += 1

    def unpin(self, page_id: int) -> None:
        frame = self._frames.get(page_id)
        if frame is not None and frame.pins > 0:
            frame.pins -= 1

    # -- allocation --------------------------------------------------------
    def allocate_page(self) -> int:
        return allocate_page_via(self.meta, self.read_page, self.write_page)

    def free_page(self, page_id: int) -> None:
        free_page_via(self.meta, page_id, self.write_page)

    # -- transaction plumbing ---------------------------------------------
    def has_writes(self) -> bool:
        return any(f.dirty for f in self._frames.values())

    def dirty_pages(self) -> dict[int, bytes]:
        """Modified pages (including the refreshed meta page 0) for WAL logging."""
        self.write_page(0, self.meta.pack())
        return {pid: bytes(f.data) for pid, f in self._frames.items() if f.dirty}

    def flush(self) -> None:
        """Write every dirty page (and meta) to the physical file and fsync, then
        mark them clean but keep them resident. Called only after the commit
        record is durable in the WAL."""
        self.write_page(0, self.meta.pack())
        dirty = [(pid, f) for pid, f in self._frames.items() if f.dirty]
        for page_id, frame in dirty:
            self._pager.write_page(page_id, frame.data)
        self._pager.sync()
        for _pid, frame in dirty:
            frame.dirty = False

    def discard(self) -> None:
        """Roll back: drop uncommitted (dirty) frames and reload meta from disk.
        Clean frames match the on-disk state and are kept."""
        for page_id in [pid for pid, f in self._frames.items() if f.dirty]:
            del self._frames[page_id]
        self._pager.meta = Meta.unpack(self._pager.read_page(0))

    def apply_raw(self, page_id: int, data: bytes) -> None:
        """Recovery hook: write an after-image straight through and drop any
        stale cached frame."""
        self._pager.write_page(page_id, data)
        self._frames.pop(page_id, None)

    # -- diagnostics -------------------------------------------------------
    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "capacity": self.capacity,
            "resident": len(self._frames),
            "dirty": sum(1 for f in self._frames.values() if f.dirty),
            "pinned": sum(1 for f in self._frames.values() if f.pins > 0),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_ratio": (self.hits / total) if total else 0.0,
        }

    def close(self) -> None:
        self._pager.close()
