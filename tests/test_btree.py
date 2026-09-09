"""Unit tests for the persistent B+Tree in isolation."""

import random

import pytest

from minidb.storage.btree import BPlusTree
from minidb.storage.pager import Pager


@pytest.fixture
def pager(tmp_path):
    p = Pager(str(tmp_path / "bt.db"))
    yield p
    p.close()


def test_insert_and_get(pager):
    tree = BPlusTree.create(pager)
    tree.insert(1, b"one")
    tree.insert(2, b"two")
    assert tree.get(1) == b"one"
    assert tree.get(2) == b"two"
    assert tree.get(3) is None


def test_upsert_replaces_value(pager):
    tree = BPlusTree.create(pager)
    tree.insert(1, b"old")
    tree.insert(1, b"new")
    assert tree.get(1) == b"new"


def test_ordered_scan_after_random_inserts(pager):
    tree = BPlusTree.create(pager)
    keys = list(range(1, 500))
    random.shuffle(keys)
    for k in keys:
        tree.insert(k, f"v{k}".encode())
    assert [k for k, _ in tree.items()] == sorted(keys)


def test_range_scan(pager):
    tree = BPlusTree.create(pager)
    for k in range(0, 100):
        tree.insert(k, str(k).encode())
    assert [k for k, _ in tree.range(10, 20)] == list(range(10, 21))
    assert [k for k, _ in tree.range(None, 3)] == [0, 1, 2, 3]
    assert [k for k, _ in tree.range(97, None)] == [97, 98, 99]


def test_delete(pager):
    tree = BPlusTree.create(pager)
    for k in range(10):
        tree.insert(k, b"x")
    assert tree.delete(5) is True
    assert tree.get(5) is None
    assert tree.delete(5) is False
    assert [k for k, _ in tree.items()] == [0, 1, 2, 3, 4, 6, 7, 8, 9]


def test_splits_create_multilevel_tree(pager):
    """Enough rows to force internal-node splits, then verify persistence."""
    tree = BPlusTree.create(pager)
    for k in range(3000):
        tree.insert(k, f"payload-{k}".encode())
    root_id = tree.root

    # reopen the tree from the same root page id -> must read back identically
    reopened = BPlusTree(pager, root_id)
    assert [k for k, _ in reopened.items()] == list(range(3000))
    assert reopened.get(2999) == b"payload-2999"


def test_large_values_spill_to_overflow(pager):
    """A value bigger than a page is stored in an overflow chain, transparently."""
    tree = BPlusTree.create(pager)
    big = b"x" * 5000       # bigger than a page
    huge = bytes(range(256)) * 4096  # ~1 MiB, many overflow pages
    tree.insert(1, b"small")
    tree.insert(2, big)
    tree.insert(3, huge)
    assert tree.get(1) == b"small"
    assert tree.get(2) == big
    assert tree.get(3) == huge
    tree.validate()
    # ordered scan reassembles overflow values too
    assert dict(tree.items()) == {1: b"small", 2: big, 3: huge}
    # replacing a large value with a small one reclaims the old chain
    freed_before = tree.pager.meta.free_list_head
    tree.insert(3, b"tiny")
    assert tree.get(3) == b"tiny"
    assert tree.pager.meta.free_list_head != freed_before  # pages returned
    tree.validate()
