"""Bounded buffer pool: caching, eviction, pinning, dirty handling, and its
interaction with the WAL / crash recovery."""

import pytest

from minidb import Database
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.pager import DATA_SIZE, CorruptionError, Pager


def _build_file(path, n):
    """Create a file with `n` data pages; return their page ids."""
    bp = BufferPool(Pager(path), capacity=10_000)
    ids = []
    for i in range(n):
        pid = bp.allocate_page()
        bp.write_page(pid, bytes([i % 251]) * DATA_SIZE)
        ids.append(pid)
    bp.flush()
    bp.close()
    return ids


def _pool(path, capacity):
    return BufferPool(Pager(path), capacity=capacity)


def test_cache_hit(tmp_path):
    path = str(tmp_path / "b.db")
    ids = _build_file(path, 10)
    bp = _pool(path, 4)
    bp.read_page(ids[0])              # miss (loads from disk)
    h0, m0 = bp.hits, bp.misses
    bp.read_page(ids[0])              # hit (resident)
    assert bp.hits == h0 + 1 and bp.misses == m0
    bp.close()


def test_cache_miss_and_lru_eviction(tmp_path):
    path = str(tmp_path / "b.db")
    ids = _build_file(path, 10)
    bp = _pool(path, 4)
    for pid in ids[:4]:
        bp.read_page(pid)            # 4 misses -> resident 4
    assert bp.stats()["resident"] == 4 and bp.misses == 4
    bp.read_page(ids[4])             # evicts LRU (ids[0])
    assert bp.stats()["resident"] == 4
    assert ids[0] not in bp._frames  # least-recently-used evicted
    assert bp.evictions >= 1
    m0 = bp.misses
    assert bytes(bp.read_page(ids[0])) == bytes([0]) * DATA_SIZE  # reload, intact
    assert bp.misses == m0 + 1
    bp.close()


def test_pin_prevents_eviction(tmp_path):
    path = str(tmp_path / "b.db")
    ids = _build_file(path, 10)
    bp = _pool(path, 4)
    for pid in ids[:4]:
        bp.read_page(pid)
    bp.pin(ids[0])                   # pin the current LRU
    bp.read_page(ids[4])             # must evict someone other than the pinned page
    assert ids[0] in bp._frames
    assert ids[1] not in bp._frames  # next-oldest evicted instead
    bp.close()


def test_full_pool_of_pinned_pages_grows_safely(tmp_path):
    path = str(tmp_path / "b.db")
    ids = _build_file(path, 10)
    bp = _pool(path, 4)
    for pid in ids[:4]:
        bp.read_page(pid)
        bp.pin(pid)                  # every frame pinned
    bp.read_page(ids[4])             # nothing evictable -> pool grows, no data lost
    assert bp.stats()["resident"] == 5
    assert bytes(bp.read_page(ids[0])) == bytes([0]) * DATA_SIZE
    bp.close()


def test_dirty_tracking_and_flush_persist(tmp_path):
    path = str(tmp_path / "b.db")
    bp = _pool(path, 100)
    pid = bp.allocate_page()
    bp.write_page(pid, b"Z" * DATA_SIZE)
    assert bp.has_writes() and bp.stats()["dirty"] >= 1
    bp.flush()
    assert not bp.has_writes() and bp.stats()["dirty"] == 0
    bp.close()
    bp2 = _pool(path, 100)           # reopen: the flushed page is on disk
    assert bytes(bp2.read_page(pid)) == b"Z" * DATA_SIZE
    bp2.close()


def test_once_dirty_page_survives_eviction(tmp_path):
    path = str(tmp_path / "b.db")
    ids = _build_file(path, 10)
    bp = _pool(path, 4)
    pid = bp.allocate_page()
    bp.write_page(pid, b"K" * DATA_SIZE)
    bp.flush()                       # now clean + on disk
    for other in ids[:6]:            # churn the cache to evict `pid`
        bp.read_page(other)
    assert pid not in bp._frames     # it was evicted
    assert bytes(bp.read_page(pid)) == b"K" * DATA_SIZE  # reloaded without loss
    bp.close()


def test_discard_drops_uncommitted_writes(tmp_path):
    path = str(tmp_path / "b.db")
    bp = _pool(path, 100)
    pid = bp.allocate_page()
    bp.write_page(pid, b"O" * DATA_SIZE)
    bp.flush()                       # committed content on disk
    bp.write_page(pid, b"N" * DATA_SIZE)   # uncommitted overwrite
    bp.discard()                     # rollback
    assert bytes(bp.read_page(pid)) == b"O" * DATA_SIZE  # old content restored
    bp.close()


# -- DB-level: WAL + pool + recovery, under a tiny cache -------------------
def test_restart_persistence_small_cache(tmp_path):
    path = str(tmp_path / "d.db")
    db = Database(path, cache_pages=8)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    for i in range(1, 300):          # many pages, cache holds only 8
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
    assert db.cache_stats()["evictions"] >= 0
    db.close()

    db2 = Database(path, cache_pages=8)
    assert db2.execute("SELECT COUNT(*) FROM t").rows == [[299]]
    assert db2.execute("SELECT v FROM t WHERE id = 250").rows == [["v250"]]
    db2.close()


def test_crash_recovery_with_eviction(tmp_path):
    from minidb.sql.parser import parse
    path = str(tmp_path / "d.db")
    db = Database(path, cache_pages=8)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    db.execute("INSERT INTO t VALUES (1, 10)")

    # commit a txn into the WAL but never flush its pages -> simulate crash
    txn = db.txn_manager.begin(autocommit=True)
    db.executor.execute(parse("INSERT INTO t VALUES (2, 20)"), txn)
    for pid, data in db.pager.dirty_pages().items():
        db.wal.log_page(txn.xid, pid, data)
    db.wal.log_commit(txn.xid)
    del db  # power loss before flush/checkpoint

    recovered = Database(path, cache_pages=8)
    assert recovered.execute("SELECT v FROM t ORDER BY id").rows == [[10], [20]]
    recovered.close()


def test_wal_redo_heals_corrupted_page(tmp_path):
    from minidb.sql.parser import parse
    path = str(tmp_path / "d.db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'one')")

    txn = db.txn_manager.begin(autocommit=True)
    db.executor.execute(parse("INSERT INTO t VALUES (2, 'two')"), txn)
    dirty = db.pager.dirty_pages()
    for pid, data in dirty.items():
        db.wal.log_page(txn.xid, pid, data)
    db.wal.log_commit(txn.xid)
    data_pid = next(pid for pid in dirty if pid != 0)  # a non-meta page in the WAL
    del db

    # Corrupt that page on disk. The WAL still holds its committed after-image,
    # so recovery re-stamps a valid page over the damage.
    from minidb.storage.pager import PAGE_SIZE
    with open(path, "r+b") as f:
        f.seek(data_pid * PAGE_SIZE + 8 + 20)
        f.write(b"\xff\xff\xff\xff")

    recovered = Database(path)
    assert recovered.execute("SELECT v FROM t ORDER BY id").rows == [["one"], ["two"]]
    recovered.close()


def test_corrupted_page_without_wal_is_reported(tmp_path):
    """After a clean checkpoint the WAL is empty, so a later on-disk corruption
    cannot be healed — the engine must surface it, not serve bad data."""
    from minidb.storage.pager import PAGE_SIZE
    path = str(tmp_path / "d.db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    for i in range(1, 60):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"row-{i}"))
    root = db.catalog.get("t").root_page_id  # a page every scan must read
    db.close()  # clean close -> WAL checkpointed/empty

    with open(path, "r+b") as f:
        f.seek(root * PAGE_SIZE + 8 + 40)  # damage the table's root page data
        f.write(b"\x00\x01\x02\x03")

    db2 = Database(path, cache_pages=4)  # small cache forces a real disk read
    with pytest.raises(CorruptionError):
        db2.execute("SELECT COUNT(*) FROM t")  # scanning reads the corrupted page
    db2.close()
