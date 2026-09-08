"""A persistent B+Tree mapping ``int`` keys to ``bytes`` values.

Every node is exactly one page. Leaves are linked left-to-right so an ordered
scan (used by ``SELECT`` and range predicates) is a cheap sequential walk.

Design notes
------------
* Keys are signed 64-bit integers (the primary key of a table row).
* Values are variable-length byte strings (a serialized row / version chain).
* Nodes split by *byte budget* rather than a fixed fan-out, which keeps the
  code simple while supporting variable-length values.
* A single value must fit within one page; larger blobs would need overflow
  pages (a deliberate, documented limitation).

Leaf page layout (little-endian)::

    type(B)=1  next_leaf(i)  num(H)  [ key(q) vlen(I) val[vlen] ] * num

Internal page layout::

    type(B)=0  num(H)  key(q)*num  child(i)*(num+1)
"""

from __future__ import annotations

import struct
from typing import Iterator, Optional

from .pager import PAGE_SIZE, Pager, NO_PAGE

_LEAF = 1
_INTERNAL = 0
_LEAF_HEADER = 1 + 4 + 2  # type, next_leaf, num
_INT_HEADER = 1 + 2  # type, num


class _Leaf:
    __slots__ = ("keys", "vals", "next_leaf")

    def __init__(self, keys=None, vals=None, next_leaf=NO_PAGE):
        self.keys: list[int] = keys or []
        self.vals: list[bytes] = vals or []
        self.next_leaf = next_leaf

    def nbytes(self) -> int:
        return _LEAF_HEADER + sum(8 + 4 + len(v) for v in self.vals)

    def serialize(self) -> bytes:
        out = bytearray()
        out += struct.pack("<BiH", _LEAF, self.next_leaf, len(self.keys))
        for k, v in zip(self.keys, self.vals):
            out += struct.pack("<qI", k, len(v)) + v
        if len(out) > PAGE_SIZE:
            raise ValueError("leaf overflow")
        return bytes(out) + b"\x00" * (PAGE_SIZE - len(out))

    @classmethod
    def deserialize(cls, data: bytes) -> "_Leaf":
        _, next_leaf, num = struct.unpack_from("<BiH", data, 0)
        pos = _LEAF_HEADER
        keys, vals = [], []
        for _ in range(num):
            k, vlen = struct.unpack_from("<qI", data, pos)
            pos += 12
            vals.append(bytes(data[pos : pos + vlen]))
            pos += vlen
            keys.append(k)
        return cls(keys, vals, next_leaf)


class _Internal:
    __slots__ = ("keys", "children")

    def __init__(self, keys=None, children=None):
        self.keys: list[int] = keys or []
        self.children: list[int] = children or []

    def serialize(self) -> bytes:
        out = bytearray()
        out += struct.pack("<BH", _INTERNAL, len(self.keys))
        for k in self.keys:
            out += struct.pack("<q", k)
        for c in self.children:
            out += struct.pack("<i", c)
        if len(out) > PAGE_SIZE:
            raise ValueError("internal overflow")
        return bytes(out) + b"\x00" * (PAGE_SIZE - len(out))

    @classmethod
    def deserialize(cls, data: bytes) -> "_Internal":
        _, num = struct.unpack_from("<BH", data, 0)
        pos = _INT_HEADER
        keys = list(struct.unpack_from("<%dq" % num, data, pos))
        pos += 8 * num
        children = list(struct.unpack_from("<%di" % (num + 1), data, pos))
        return cls(keys, children)


def _is_leaf(data: bytes) -> bool:
    return data[0] == _LEAF


# max internal fan-out that always fits in a page (fixed-size entries)
_MAX_INT_KEYS = (PAGE_SIZE - _INT_HEADER - 4) // 12


class BPlusTree:
    def __init__(self, pager: Pager, root_page_id: int):
        self.pager = pager
        self.root = root_page_id

    # -- construction ------------------------------------------------------
    @classmethod
    def create(cls, pager: Pager) -> "BPlusTree":
        root = pager.allocate_page()
        pager.write_page(root, _Leaf().serialize())
        return cls(pager, root)

    def _load(self, page_id: int):
        data = bytes(self.pager.read_page(page_id))
        return _Leaf.deserialize(data) if _is_leaf(data) else _Internal.deserialize(data)

    def _store(self, page_id: int, node) -> None:
        self.pager.write_page(page_id, node.serialize())

    # -- point lookup ------------------------------------------------------
    def get(self, key: int) -> Optional[bytes]:
        page_id = self.root
        node = self._load(page_id)
        while isinstance(node, _Internal):
            page_id = node.children[self._child_index(node, key)]
            node = self._load(page_id)
        for k, v in zip(node.keys, node.vals):
            if k == key:
                return v
        return None

    @staticmethod
    def _child_index(node: _Internal, key: int) -> int:
        i = 0
        while i < len(node.keys) and key >= node.keys[i]:
            i += 1
        return i

    # -- insert / upsert ---------------------------------------------------
    def insert(self, key: int, value: bytes) -> None:
        split = self._insert(self.root, key, value)
        if split is not None:
            sep_key, right_id = split
            new_root = _Internal(keys=[sep_key], children=[self.root, right_id])
            new_root_id = self.pager.allocate_page()
            self._store(new_root_id, new_root)
            self.root = new_root_id

    def _insert(self, page_id: int, key: int, value: bytes):
        node = self._load(page_id)
        if isinstance(node, _Leaf):
            self._leaf_upsert(node, key, value)
            if node.nbytes() <= PAGE_SIZE:
                self._store(page_id, node)
                return None
            return self._split_leaf(page_id, node)

        idx = self._child_index(node, key)
        child_id = node.children[idx]
        split = self._insert(child_id, key, value)
        if split is None:
            return None
        sep_key, right_id = split
        node.keys.insert(idx, sep_key)
        node.children.insert(idx + 1, right_id)
        if len(node.keys) <= _MAX_INT_KEYS:
            self._store(page_id, node)
            return None
        return self._split_internal(page_id, node)

    @staticmethod
    def _leaf_upsert(node: _Leaf, key: int, value: bytes) -> None:
        lo, hi = 0, len(node.keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if node.keys[mid] < key:
                lo = mid + 1
            else:
                hi = mid
        if lo < len(node.keys) and node.keys[lo] == key:
            node.vals[lo] = value
        else:
            node.keys.insert(lo, key)
            node.vals.insert(lo, value)

    def _split_leaf(self, page_id: int, node: _Leaf):
        if len(node.keys) == 1:
            raise ValueError("value too large to fit in a page")
        mid = len(node.keys) // 2
        right = _Leaf(node.keys[mid:], node.vals[mid:], node.next_leaf)
        left = _Leaf(node.keys[:mid], node.vals[:mid])
        right_id = self.pager.allocate_page()
        left.next_leaf = right_id
        self._store(right_id, right)
        self._store(page_id, left)
        return (right.keys[0], right_id)

    def _split_internal(self, page_id: int, node: _Internal):
        mid = len(node.keys) // 2
        sep_key = node.keys[mid]
        right = _Internal(node.keys[mid + 1 :], node.children[mid + 1 :])
        left = _Internal(node.keys[:mid], node.children[: mid + 1])
        right_id = self.pager.allocate_page()
        self._store(right_id, right)
        self._store(page_id, left)
        return (sep_key, right_id)

    # -- delete ------------------------------------------------------------
    # A tombstone-free structural delete. To keep the implementation focused we
    # do not merge underflowing nodes (leaves may become sparse); correctness
    # and ordered iteration are preserved. Rows are usually removed logically by
    # the MVCC layer instead, so physical deletes are rare.
    def delete(self, key: int) -> bool:
        return self._delete(self.root, key)

    def _delete(self, page_id: int, key: int) -> bool:
        node = self._load(page_id)
        if isinstance(node, _Leaf):
            for i, k in enumerate(node.keys):
                if k == key:
                    del node.keys[i]
                    del node.vals[i]
                    self._store(page_id, node)
                    return True
            return False
        idx = self._child_index(node, key)
        return self._delete(node.children[idx], key)

    # -- ordered iteration -------------------------------------------------
    def _leftmost_leaf(self) -> int:
        page_id = self.root
        node = self._load(page_id)
        while isinstance(node, _Internal):
            page_id = node.children[0]
            node = self._load(page_id)
        return page_id

    def items(self) -> Iterator[tuple[int, bytes]]:
        page_id = self._leftmost_leaf()
        while page_id != NO_PAGE:
            leaf = self._load(page_id)
            for k, v in zip(leaf.keys, leaf.vals):
                yield k, v
            page_id = leaf.next_leaf

    def range(self, lo: Optional[int], hi: Optional[int]) -> Iterator[tuple[int, bytes]]:
        """Yield entries with ``lo <= key <= hi`` (either bound may be None)."""
        page_id = self.root
        node = self._load(page_id)
        while isinstance(node, _Internal):
            page_id = node.children[0 if lo is None else self._child_index(node, lo)]
            node = self._load(page_id)
        while page_id != NO_PAGE:
            leaf = self._load(page_id)
            for k, v in zip(leaf.keys, leaf.vals):
                if lo is not None and k < lo:
                    continue
                if hi is not None and k > hi:
                    return
                yield k, v
            page_id = leaf.next_leaf
