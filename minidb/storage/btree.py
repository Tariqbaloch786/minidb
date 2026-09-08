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

from .pager import NO_PAGE, PAGE_SIZE, Pager

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

    def nbytes(self) -> int:
        return _INT_HEADER + 8 * len(self.keys) + 4 * len(self.children)

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
    # A full structural delete: underflowing nodes borrow from a sibling
    # (redistribution) or merge with one, freeing the emptied page, and the root
    # collapses when it is reduced to a single child. The B+Tree invariants
    # (see ``validate``) hold after any sequence of inserts and deletes.
    #
    # Underflow is measured by *byte* fill, because values are variable length:
    # a node below half a page is a candidate to be re-balanced. The lone root is
    # never considered underfull (a tree may legitimately be nearly empty).
    def delete(self, key: int) -> bool:
        found, _ = self._delete(self.root, key, is_root=True)
        root = self._load(self.root)
        if isinstance(root, _Internal) and len(root.keys) == 0:
            # root has a single child: collapse a level, freeing the old root
            only_child = root.children[0]
            self.pager.free_page(self.root)
            self.root = only_child
        return found

    def _delete(self, page_id: int, key: int, is_root: bool = False):
        """Delete ``key`` from the subtree at ``page_id``.

        Returns ``(found, underfull)`` where ``underfull`` tells the caller the
        child dropped below the minimum fill and must be re-balanced.
        """
        node = self._load(page_id)
        if isinstance(node, _Leaf):
            found = False
            for i, k in enumerate(node.keys):
                if k == key:
                    del node.keys[i]
                    del node.vals[i]
                    found = True
                    break
            self._store(page_id, node)
            return found, self._leaf_underfull(node, is_root)

        idx = self._child_index(node, key)
        found, child_underfull = self._delete(node.children[idx], key)
        if child_underfull:
            self._rebalance(node, idx)
        self._store(page_id, node)
        return found, self._internal_underfull(node, is_root)

    @staticmethod
    def _leaf_underfull(node: _Leaf, is_root: bool) -> bool:
        return (not is_root) and node.nbytes() < PAGE_SIZE // 2

    @staticmethod
    def _internal_underfull(node: _Internal, is_root: bool) -> bool:
        return (not is_root) and len(node.keys) < max(1, _MAX_INT_KEYS // 2)

    def _rebalance(self, parent: _Internal, idx: int) -> None:
        """Fix ``parent.children[idx]`` after it underflowed: merge with a
        sibling if their contents fit one page, otherwise redistribute."""
        child = self._load(parent.children[idx])
        left = self._load(parent.children[idx - 1]) if idx > 0 else None
        right = (self._load(parent.children[idx + 1])
                 if idx + 1 < len(parent.children) else None)

        # Prefer merging (it reclaims a page) when the result fits in one page.
        if right is not None and self._merge_fits(child, right):
            self._merge(parent, idx, child, right, parent.children[idx + 1])
        elif left is not None and self._merge_fits(left, child):
            self._merge(parent, idx - 1, left, child, parent.children[idx])
        elif right is not None and (left is None or right.nbytes() >= left.nbytes()):
            self._borrow_right(parent, idx, child, right, parent.children[idx + 1])
        elif left is not None:
            self._borrow_left(parent, idx, child, left, parent.children[idx - 1])
        # else: child is the only child of the root — root collapse handles it.

    @staticmethod
    def _merge_fits(left, right) -> bool:
        if isinstance(left, _Leaf):
            return left.nbytes() + right.nbytes() - _LEAF_HEADER <= PAGE_SIZE
        total_keys = len(left.keys) + 1 + len(right.keys)  # +1 pulled-down separator
        return _INT_HEADER + 8 * total_keys + 4 * (total_keys + 1) <= PAGE_SIZE

    def _merge(self, parent: _Internal, sep: int, left, right, right_id: int) -> None:
        """Merge the two children on either side of ``parent.keys[sep]`` into the
        left node, free the right page, and drop the separator."""
        left_id = parent.children[sep]
        if isinstance(left, _Leaf):
            left.keys += right.keys
            left.vals += right.vals
            left.next_leaf = right.next_leaf
        else:
            left.keys.append(parent.keys[sep])  # pull the separator down
            left.keys += right.keys
            left.children += right.children
        self._store(left_id, left)
        self.pager.free_page(right_id)
        del parent.keys[sep]
        del parent.children[sep + 1]

    def _borrow_right(self, parent, idx, child, right, right_id) -> None:
        """Move entries from the right sibling into ``child`` until it is filled
        past the underflow line, fixing the separator ``parent.keys[idx]``."""
        child_id = parent.children[idx]
        if isinstance(child, _Leaf):
            while (child.nbytes() < PAGE_SIZE // 2 and len(right.keys) > 1
                   and child.nbytes() + 12 + len(right.vals[0]) <= PAGE_SIZE):
                child.keys.append(right.keys.pop(0))
                child.vals.append(right.vals.pop(0))
            parent.keys[idx] = right.keys[0]
        else:
            while len(child.keys) < max(1, _MAX_INT_KEYS // 2) and len(right.keys) > 1:
                child.keys.append(parent.keys[idx])
                child.children.append(right.children.pop(0))
                parent.keys[idx] = right.keys.pop(0)
        self._store(child_id, child)
        self._store(right_id, right)

    def _borrow_left(self, parent, idx, child, left, left_id) -> None:
        """Symmetric to :meth:`_borrow_right`, pulling from the left sibling and
        fixing the separator ``parent.keys[idx - 1]``."""
        child_id = parent.children[idx]
        if isinstance(child, _Leaf):
            while (child.nbytes() < PAGE_SIZE // 2 and len(left.keys) > 1
                   and child.nbytes() + 12 + len(left.vals[-1]) <= PAGE_SIZE):
                child.keys.insert(0, left.keys.pop())
                child.vals.insert(0, left.vals.pop())
            parent.keys[idx - 1] = child.keys[0]
        else:
            while len(child.keys) < max(1, _MAX_INT_KEYS // 2) and len(left.keys) > 1:
                child.keys.insert(0, parent.keys[idx - 1])
                child.children.insert(0, left.children.pop())
                parent.keys[idx - 1] = left.keys.pop()
        self._store(child_id, child)
        self._store(left_id, left)

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

    # -- integrity check ---------------------------------------------------
    def validate(self) -> bool:
        """Assert every B+Tree structural invariant; return True if well-formed.

        Invariants checked:

        * keys are strictly sorted within every node and lie inside the
          ``[low, high)`` range implied by their ancestors' separators;
        * every internal node has exactly ``len(keys) + 1`` children and at
          least one separator;
        * all leaves are at the same depth (the tree is height-balanced);
        * the left-to-right ``next_leaf`` chain visits exactly the leaves found
          by an in-order descent, in the same order, with no cycle;
        * concatenating the leaves yields a globally sorted key sequence.

        Raises :class:`AssertionError` (with a description) on any violation.
        """
        leaves_in_order: list[int] = []

        def check(page_id: int, lo, hi, depth: int) -> int:
            node = self._load(page_id)
            if isinstance(node, _Leaf):
                for i, k in enumerate(node.keys):
                    assert lo is None or k >= lo, f"leaf key {k} < lower bound {lo}"
                    assert hi is None or k < hi, f"leaf key {k} >= upper bound {hi}"
                    assert i == 0 or node.keys[i - 1] < k, "leaf keys not sorted"
                leaves_in_order.append(page_id)
                return depth
            assert len(node.children) == len(node.keys) + 1, \
                f"internal node {page_id}: children != keys + 1"
            assert len(node.keys) >= 1, f"internal node {page_id} has no separators"
            for i in range(1, len(node.keys)):
                assert node.keys[i - 1] < node.keys[i], "separators not sorted"
            depths = set()
            for i, cid in enumerate(node.children):
                clo = lo if i == 0 else node.keys[i - 1]
                chi = hi if i == len(node.children) - 1 else node.keys[i]
                depths.add(check(cid, clo, chi, depth + 1))
            assert len(depths) == 1, f"tree unbalanced: differing leaf depths {depths}"
            return depths.pop()

        check(self.root, None, None, 0)

        # The sibling chain must agree with the in-order leaf sequence.
        chain: list[int] = []
        all_keys: list[int] = []
        page_id = self._leftmost_leaf()
        limit = len(leaves_in_order) + 1
        while page_id != NO_PAGE:
            assert len(chain) < limit, "cycle detected in leaf next_leaf chain"
            leaf = self._load(page_id)
            chain.append(page_id)
            all_keys.extend(leaf.keys)
            page_id = leaf.next_leaf
        assert chain == leaves_in_order, "leaf chain disagrees with tree order"
        assert all_keys == sorted(all_keys), "global key order violated"
        return True
