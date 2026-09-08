"""Multi-Version Concurrency Control (snapshot isolation).

Every row is stored not as a single value but as a *version chain*: a list of
row versions, each tagged with the transaction that created it (``xmin``) and,
once superseded or deleted, the transaction that retired it (``xmax``). A
transaction reads through a **snapshot** taken when it began, so it always sees
a consistent point-in-time view and never sees another transaction's
uncommitted work. ``ROLLBACK`` is honoured because a transaction's page writes
are buffered and simply discarded (see :class:`minidb.database.Database`).

Durability model note: only *committed* transactions' pages are ever flushed to
the main file (WAL-first, force-at-commit), so any version found on disk after a
restart was necessarily written by a committed transaction. That is why we do
not need a persistent commit log — an ``xmin`` we have never heard of is, by
construction, committed.

This engine runs a single active writer at a time; the visibility machinery and
version chains are the real thing, while multi-writer concurrency control is a
documented next step.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

COMMITTED = "committed"
ABORTED = "aborted"
ACTIVE = "active"


@dataclass
class Version:
    xmin: int
    xmax: int  # 0 == still live
    deleted: bool
    data: bytes  # serialized row (empty for a tombstone)


# -- version-chain (de)serialization ---------------------------------------
def encode_chain(chain: list[Version]) -> bytes:
    out = bytearray(struct.pack("<H", len(chain)))
    for v in chain:
        flags = 1 if v.deleted else 0
        out += struct.pack("<QQBI", v.xmin, v.xmax, flags, len(v.data)) + v.data
    return bytes(out)


def decode_chain(data: bytes) -> list[Version]:
    (count,) = struct.unpack_from("<H", data, 0)
    pos = 2
    chain: list[Version] = []
    for _ in range(count):
        xmin, xmax, flags, length = struct.unpack_from("<QQBI", data, pos)
        pos += 21
        payload = bytes(data[pos : pos + length])
        pos += length
        chain.append(Version(xmin, xmax, bool(flags & 1), payload))
    return chain


class Transaction:
    def __init__(self, xid: int, snapshot_xmax: int, active: frozenset[int], autocommit: bool):
        self.xid = xid
        self.snapshot_xmax = snapshot_xmax
        self.snapshot_active = active
        self.autocommit = autocommit
        self.state = ACTIVE


class TransactionManager:
    """Allocates transaction ids and answers visibility questions."""

    def __init__(self, pager):
        self.pager = pager
        self.status: dict[int, str] = {}
        self.active: dict[int, Transaction] = {}

    def _next_xid(self) -> int:
        xid = self.pager.meta.next_txid
        self.pager.meta.next_txid += 1
        return xid

    def begin(self, autocommit: bool = False) -> Transaction:
        active_ids = frozenset(self.active.keys())
        xid = self._next_xid()
        txn = Transaction(xid, xid, active_ids, autocommit)
        self.status[xid] = ACTIVE
        self.active[xid] = txn
        return txn

    def commit(self, txn: Transaction) -> None:
        self.status[txn.xid] = COMMITTED
        txn.state = COMMITTED
        self.active.pop(txn.xid, None)

    def abort(self, txn: Transaction) -> None:
        self.status[txn.xid] = ABORTED
        txn.state = ABORTED
        self.active.pop(txn.xid, None)

    # -- visibility --------------------------------------------------------
    def _is_committed(self, xid: int) -> bool:
        # An xid we have no record of came from a prior process run; by the
        # durability model it can only be on disk if it committed.
        return self.status.get(xid, COMMITTED) == COMMITTED

    def _created_visible(self, xmin: int, txn: Transaction) -> bool:
        if xmin == txn.xid:
            return True
        if xmin in txn.snapshot_active:
            return False
        if xmin >= txn.snapshot_xmax:
            return False
        return self._is_committed(xmin)

    def _retire_visible(self, xmax: int, txn: Transaction) -> bool:
        if xmax == 0:
            return False
        if xmax == txn.xid:
            return True
        if xmax in txn.snapshot_active:
            return False
        if xmax >= txn.snapshot_xmax:
            return False
        return self._is_committed(xmax)

    def visible_version(self, chain: list[Version], txn: Transaction) -> Optional[Version]:
        """Return the row version visible to ``txn`` (or None if the row is gone)."""
        best: Optional[Version] = None
        for v in chain:  # oldest -> newest; last created-visible wins
            if self._created_visible(v.xmin, txn):
                best = v
        if best is None or best.deleted:
            return None
        if self._retire_visible(best.xmax, txn):
            return None
        return best

    def visible_row(self, chain: list[Version], txn: Transaction) -> Optional[bytes]:
        v = self.visible_version(chain, txn)
        return v.data if v is not None else None

    # -- vacuum ------------------------------------------------------------
    def _oldest_snapshot_xmax(self) -> int:
        if not self.active:
            return self.pager.meta.next_txid
        return min(t.snapshot_xmax for t in self.active.values())

    def compact(self, chain: list[Version]) -> list[Version]:
        """Drop versions no live snapshot could ever see again."""
        horizon = self._oldest_snapshot_xmax()
        kept: list[Version] = []
        for v in chain:
            dead = v.xmax != 0 and self._is_committed(v.xmax) and v.xmax < horizon
            if dead:
                continue
            kept.append(v)
        return kept
