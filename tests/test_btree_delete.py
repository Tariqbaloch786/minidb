"""B+Tree deletion: rebalancing, root collapse, page reuse, and the structural
``validate()`` invariant — exercised with randomized/property tests.

Values are deliberately chunky (~200 bytes) so the tree grows several levels
deep, which forces node merges, redistribution, and root collapse to actually
fire during deletion rather than staying in the happy path.
"""

import random

import pytest

from minidb.storage.btree import BPlusTree
from minidb.storage.pager import Pager


@pytest.fixture
def pager(tmp_path):
    p = Pager(str(tmp_path / "bt.db"))
    yield p
    p.close()


def _val(k: int) -> bytes:
    return f"row-{k}-".encode() + b"x" * 200


def test_delete_basic(pager):
    t = BPlusTree.create(pager)
    for k in range(1, 51):
        t.insert(k, _val(k))
    assert t.delete(25) is True
    assert t.get(25) is None
    assert t.delete(25) is False   # already gone
    assert t.delete(999) is False  # never existed
    t.validate()
    assert [k for k, _ in t.items()] == [k for k in range(1, 51) if k != 25]


def test_delete_all_leaves_empty_valid_tree(pager):
    t = BPlusTree.create(pager)
    keys = list(range(1, 400))
    for k in keys:
        t.insert(k, _val(k))
    rng = random.Random(0)
    rng.shuffle(keys)
    for k in keys:
        assert t.delete(k) is True
    t.validate()
    assert list(t.items()) == []
    assert t.get(1) is None
    # deleting keys can be resumed with fresh inserts on the collapsed tree
    t.insert(42, _val(42))
    t.validate()
    assert t.get(42) == _val(42)


def test_validate_catches_a_broken_node(pager):
    """validate() must actually reject a corrupted structure, not rubber-stamp."""
    t = BPlusTree.create(pager)
    for k in range(1, 300):
        t.insert(k, _val(k))
    t.validate()  # healthy
    leaf_id = t._leftmost_leaf()
    leaf = t._load(leaf_id)
    assert len(leaf.keys) >= 2
    leaf.keys[0], leaf.keys[1] = leaf.keys[1], leaf.keys[0]  # break ordering
    t._store(leaf_id, leaf)
    with pytest.raises(AssertionError):
        t.validate()


@pytest.mark.parametrize("seed", [1, 2, 3, 7, 42, 99])
def test_fuzz_insert_delete_keeps_tree_valid(pager, seed):
    t = BPlusTree.create(pager)
    rng = random.Random(seed)
    ref: dict[int, bytes] = {}
    for i in range(2500):
        k = rng.randint(1, 400)
        if rng.random() < 0.5:
            v = _val(k) + str(rng.random()).encode()
            t.insert(k, v)
            ref[k] = v
        else:
            assert t.delete(k) == (k in ref)
            ref.pop(k, None)
        if i % 50 == 0:
            t.validate()
            assert [kk for kk, _ in t.items()] == sorted(ref)
    t.validate()
    assert dict(t.items()) == ref


def test_mission_scenario(pager):
    """The exact stress the brief asks for: 10k in, 5k out, more in, more out."""
    t = BPlusTree.create(pager)
    ref: dict[int, bytes] = {}
    for k in range(1, 10001):
        v = _val(k)
        t.insert(k, v)
        ref[k] = v
    t.validate()

    rng = random.Random(123)
    for k in rng.sample(range(1, 10001), 5000):
        t.delete(k)
        ref.pop(k, None)
    t.validate()

    for k in range(10001, 12001):
        v = _val(k)
        t.insert(k, v)
        ref[k] = v
    t.validate()

    for k in rng.sample(range(1, 12001), 3000):
        t.delete(k)
        ref.pop(k, None)
    t.validate()

    assert [kk for kk, _ in t.items()] == sorted(ref)
    assert dict(t.items()) == ref


def test_deletion_frees_and_reuses_pages(pager):
    t = BPlusTree.create(pager)
    for k in range(1, 2001):
        t.insert(k, _val(k))
    peak = pager.meta.num_pages
    for k in range(1, 2001):
        assert t.delete(k) is True
    t.validate()
    # deletion returned pages to the free list
    assert pager.meta.free_list_head != -1
    # re-inserting reuses freed pages instead of growing the file unbounded
    for k in range(1, 2001):
        t.insert(k, _val(k))
    t.validate()
    assert pager.meta.num_pages <= peak + 10
