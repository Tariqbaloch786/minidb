"""Transaction, MVCC, durability and crash-recovery tests."""

from minidb import Database
from minidb.sql.parser import parse


def test_rollback_discards_changes(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db.execute("UPDATE t SET v = 'z' WHERE id = 1")
    assert len(db.execute("SELECT * FROM t").rows) == 2
    db.execute("ROLLBACK")
    assert db.execute("SELECT * FROM t ORDER BY id").rows == [[1, "a"]]


def test_commit_persists_changes(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db.execute("COMMIT")
    assert db.execute("SELECT * FROM t").rows == [[1, "a"]]


def test_delete_then_reinsert_same_key(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db.execute("DELETE FROM t WHERE id = 1")
    db.execute("INSERT INTO t VALUES (1, 'b')")  # key is free again
    assert db.execute("SELECT v FROM t WHERE id = 1").rows == [["b"]]


def test_durability_across_reopen(tmp_path):
    path = str(tmp_path / "d.db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'persisted')")
    db.close()

    db2 = Database(path)
    assert db2.execute("SELECT * FROM t").rows == [[1, "persisted"]]
    db2.close()


def _crash_after_commit_record(db, sql):
    """Log a committed txn to the WAL but skip the page flush + checkpoint."""
    txn = db.txn_manager.begin(autocommit=True)
    db.executor.execute(parse(sql), txn)
    for pid, data in db.pager.dirty_pages().items():
        db.wal.log_page(txn.xid, pid, data)
    db.wal.log_commit(txn.xid)
    db.wal._f.flush()
    db.pager._f.flush()


def test_recovery_redoes_committed_txn(tmp_path):
    path = str(tmp_path / "r.db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'before')")
    _crash_after_commit_record(db, "INSERT INTO t VALUES (2, 'in-wal')")
    del db  # simulate power loss (no clean close)

    recovered = Database(path)
    assert recovered.execute("SELECT * FROM t ORDER BY id").rows == [
        [1, "before"],
        [2, "in-wal"],
    ]
    recovered.close()


def test_recovery_discards_uncommitted_txn(tmp_path):
    path = str(tmp_path / "a.db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("INSERT INTO t VALUES (1, 'keep')")

    # page records written but NO commit record -> must be rolled back on recovery
    txn = db.txn_manager.begin(autocommit=True)
    db.executor.execute(parse("INSERT INTO t VALUES (2, 'gone')"), txn)
    for pid, data in db.pager.dirty_pages().items():
        db.wal.log_page(txn.xid, pid, data)
    db.wal._f.flush()
    del db

    recovered = Database(path)
    assert recovered.execute("SELECT * FROM t").rows == [[1, "keep"]]
    recovered.close()


def test_mvcc_version_chain_visibility(db):
    """A snapshot taken before an update should not see the new version."""
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    db.execute("INSERT INTO t VALUES (1, 100)")

    # Open a long-running reader snapshot.
    reader = db.txn_manager.begin(autocommit=False)

    # A separate autocommit writer bumps the value and commits.
    db.execute("UPDATE t SET v = 200 WHERE id = 1")

    # The reader's snapshot predates the writer, so it still sees 100.
    plan_rows = _rows_for(db, reader, "SELECT v FROM t WHERE id = 1")
    assert plan_rows == [[100]]

    db.txn_manager.commit(reader)
    # A fresh statement sees the committed new value.
    assert db.execute("SELECT v FROM t WHERE id = 1").rows == [[200]]


def _rows_for(db, txn, sql):
    return db.executor.execute(parse(sql), txn).rows
