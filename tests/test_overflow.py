"""Overflow pages: values larger than one page are stored transparently."""

import pytest

from minidb import Database


def _blob(tag, size):
    return f"{tag}-" + "y" * size


@pytest.mark.parametrize("size", [4 * 1024, 16 * 1024, 64 * 1024, 1024 * 1024])
def test_large_value_roundtrips(db, size):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
    blob = _blob("v", size)
    db.execute("INSERT INTO t VALUES (?, ?)", (1, blob))
    db.execute("INSERT INTO t VALUES (?, ?)", (2, "small"))
    assert db.execute("SELECT body FROM t WHERE id = 1").rows[0][0] == blob
    assert db.execute("SELECT body FROM t WHERE id = 2").rows[0][0] == "small"


def test_scan_mixes_inline_and_overflow(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
    expected = {}
    for i in range(1, 21):
        val = _blob(f"r{i}", 3000 * (i % 4))  # some inline, some overflown
        db.execute("INSERT INTO t VALUES (?, ?)", (i, val))
        expected[i] = val
    got = {r[0]: r[1] for r in db.execute("SELECT id, body FROM t").rows}
    assert got == expected


def test_update_large_to_small_and_back(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO t VALUES (?, ?)", (1, _blob("big", 50_000)))
    db.execute("UPDATE t SET body = ? WHERE id = 1", ("tiny",))
    assert db.execute("SELECT body FROM t WHERE id = 1").rows[0][0] == "tiny"
    big2 = _blob("big2", 80_000)
    db.execute("UPDATE t SET body = ? WHERE id = 1", (big2,))
    assert db.execute("SELECT body FROM t WHERE id = 1").rows[0][0] == big2


def test_overflow_pages_are_reused_not_leaked(db):
    """Repeated insert/delete of a large value must not grow the file without
    bound — freed overflow pages are recycled (a ~200 KiB blob is ~50 pages, so
    10 cycles would be ~500 pages if leaked)."""
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO t VALUES (?, ?)", (1, _blob("a", 200_000)))
    db.execute("DELETE FROM t WHERE id = 1")
    baseline = db.pager.meta.num_pages
    for _ in range(10):
        db.execute("INSERT INTO t VALUES (?, ?)", (1, _blob("a", 200_000)))
        db.execute("DELETE FROM t WHERE id = 1")
    assert db.pager.meta.num_pages <= baseline + 120  # reused, not ~500 leaked
    assert db.execute("SELECT COUNT(*) FROM t").rows == [[0]]


def test_rollback_of_large_insert_leaves_nothing(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
    db.execute("INSERT INTO t VALUES (?, ?)", (1, "keep"))
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (?, ?)", (2, _blob("ghost", 300_000)))
    db.execute("ROLLBACK")
    assert db.execute("SELECT id FROM t").rows == [[1]]
    assert db.execute("SELECT body FROM t WHERE id = 1").rows[0][0] == "keep"


def test_large_values_persist_across_reopen(tmp_path):
    path = str(tmp_path / "ov.db")
    db = Database(path, cache_pages=16)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, body TEXT)")
    blobs = {i: _blob(f"d{i}", i * 40_000) for i in range(1, 6)}
    for i, b in blobs.items():
        db.execute("INSERT INTO t VALUES (?, ?)", (i, b))
    db.close()

    db2 = Database(path, cache_pages=16)
    for i, b in blobs.items():
        assert db2.execute("SELECT body FROM t WHERE id = ?", (i,)).rows[0][0] == b
    db2.close()


def test_index_lookup_returns_overflow_row(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, sku TEXT, body TEXT)")
    big = _blob("body", 120_000)
    db.execute("INSERT INTO t VALUES (?, ?, ?)", (1, "SKU-1", big))
    db.execute("INSERT INTO t VALUES (?, ?, ?)", (2, "SKU-2", "small"))
    db.execute("CREATE INDEX idx_sku ON t(sku)")
    plan = db.execute("EXPLAIN SELECT body FROM t WHERE sku = 'SKU-1'").rows[0][0]
    assert "Index Scan on idx_sku" in plan
    assert db.execute("SELECT body FROM t WHERE sku = 'SKU-1'").rows[0][0] == big
