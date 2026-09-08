"""Secondary index tests: creation, equality/range scans, uniqueness,
maintenance under UPDATE/DELETE, planner selection, persistence, and MVCC."""

import random

import pytest

from minidb import Database
from minidb.engine.executor import ExecutionError


def seed(db, n=40):
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, email TEXT, age INT)")
    for i in range(1, n + 1):
        db.execute("INSERT INTO users VALUES (?, ?, ?)", (i, f"u{i}@x.io", 20 + i % 7))


def test_create_index_and_explain_uses_it(db):
    seed(db)
    db.execute("CREATE INDEX idx_email ON users(email)")
    plan = db.execute("EXPLAIN SELECT * FROM users WHERE email = 'u5@x.io'").rows[0][0]
    assert "Index Scan on idx_email" in plan


def test_equality_lookup_matches_seq_scan(db):
    seed(db)
    before = db.execute("SELECT id FROM users WHERE email = 'u9@x.io'").rows
    db.execute("CREATE INDEX idx_email ON users(email)")
    after = db.execute("SELECT id FROM users WHERE email = 'u9@x.io'").rows
    assert before == after == [[9]]


def test_range_scan_via_index(db):
    seed(db)
    db.execute("CREATE INDEX idx_age ON users(age)")
    plan = db.execute("EXPLAIN SELECT id FROM users WHERE age >= 25").rows[0][0]
    assert "Index Scan on idx_age" in plan
    got = sorted(x[0] for x in db.execute("SELECT id FROM users WHERE age >= 25").rows)
    expect = sorted(i for i in range(1, 41) if 20 + i % 7 >= 25)
    assert got == expect


def test_unique_index_rejects_duplicate_insert(db):
    seed(db)
    db.execute("CREATE UNIQUE INDEX idx_email_u ON users(email)")
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO users VALUES (999, 'u3@x.io', 40)")


def test_unique_index_rejects_duplicate_update(db):
    seed(db)
    db.execute("CREATE UNIQUE INDEX idx_email_u ON users(email)")
    with pytest.raises(ExecutionError):
        db.execute("UPDATE users SET email = 'u1@x.io' WHERE id = 2")
    # updating a row to its own value is fine
    db.execute("UPDATE users SET email = 'u2@x.io' WHERE id = 2")


def test_create_unique_index_on_duplicate_data_fails(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    db.execute("INSERT INTO t VALUES (1, 5), (2, 5)")
    with pytest.raises(ExecutionError):
        db.execute("CREATE UNIQUE INDEX idx_v ON t(v)")


def test_unique_allows_multiple_nulls(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    db.execute("CREATE UNIQUE INDEX idx_v ON t(v)")
    db.execute("INSERT INTO t VALUES (1, NULL)")
    db.execute("INSERT INTO t VALUES (2, NULL)")  # multiple NULLs are allowed
    assert db.execute("SELECT COUNT(*) FROM t").rows == [[2]]


def test_non_unique_index_allows_duplicates(db):
    seed(db)
    db.execute("CREATE INDEX idx_age ON users(age)")
    db.execute("INSERT INTO users VALUES (500, 'x@x.io', 20)")
    db.execute("INSERT INTO users VALUES (501, 'y@x.io', 20)")
    got = sorted(x[0] for x in db.execute("SELECT id FROM users WHERE age = 20").rows)
    assert 500 in got and 501 in got


def test_update_maintains_index(db):
    seed(db)
    db.execute("CREATE INDEX idx_email ON users(email)")
    db.execute("UPDATE users SET email = 'moved@x.io' WHERE id = 3")
    assert db.execute("SELECT id FROM users WHERE email = 'u3@x.io'").rows == []
    assert db.execute("SELECT id FROM users WHERE email = 'moved@x.io'").rows == [[3]]


def test_delete_leaves_no_phantom_via_index(db):
    seed(db)
    db.execute("CREATE INDEX idx_email ON users(email)")
    db.execute("DELETE FROM users WHERE id = 5")
    assert db.execute("SELECT id FROM users WHERE email = 'u5@x.io'").rows == []


def test_composite_index_equality(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, a INT, b TEXT)")
    db.execute("INSERT INTO t VALUES (1, 1, 'x'), (2, 1, 'y'), (3, 2, 'x')")
    db.execute("CREATE INDEX idx_ab ON t(a, b)")
    plan = db.execute("EXPLAIN SELECT id FROM t WHERE a = 1").rows[0][0]
    assert "Index Scan on idx_ab" in plan
    assert sorted(x[0] for x in db.execute("SELECT id FROM t WHERE a = 1").rows) == [1, 2]


def test_drop_index(db):
    seed(db)
    db.execute("CREATE INDEX idx_email ON users(email)")
    db.execute("DROP INDEX idx_email")
    plan = db.execute("EXPLAIN SELECT * FROM users WHERE email = 'u1@x.io'").rows[0][0]
    assert "Seq Scan" in plan
    with pytest.raises(ExecutionError):
        db.execute("DROP INDEX idx_email")  # already gone


def test_pk_predicate_still_preferred_over_index(db):
    seed(db)
    db.execute("CREATE INDEX idx_age ON users(age)")
    plan = db.execute("EXPLAIN SELECT * FROM users WHERE id = 4 AND age = 20").rows[0][0]
    assert "Index Seek on users_pkey" in plan  # PK access, not the secondary index


def test_index_persists_across_reopen(tmp_path):
    path = str(tmp_path / "idx.db")
    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, email TEXT)")
    for i in range(1, 30):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"e{i}@x.io"))
    db.execute("CREATE UNIQUE INDEX idx_email ON t(email)")
    db.close()

    db2 = Database(path)
    plan = db2.execute("EXPLAIN SELECT id FROM t WHERE email = 'e10@x.io'").rows[0][0]
    assert "Index Scan on idx_email" in plan
    assert db2.execute("SELECT id FROM t WHERE email = 'e10@x.io'").rows == [[10]]
    with pytest.raises(ExecutionError):
        db2.execute("INSERT INTO t VALUES (99, 'e10@x.io')")  # uniqueness survived
    db2.close()


def test_index_scan_respects_mvcc_snapshot(db):
    seed(db, n=10)
    db.execute("CREATE INDEX idx_email ON users(email)")
    db.execute("BEGIN")
    # a reader in this txn takes a snapshot; simulate by reading, then... in this
    # single-connection engine, an index lookup must still not return a row whose
    # visible version does not match. Insert a row and roll it back:
    db.execute("INSERT INTO users VALUES (77, 'ghost@x.io', 30)")
    db.execute("ROLLBACK")
    # the rolled-back row must be invisible through the index too
    assert db.execute("SELECT id FROM users WHERE email = 'ghost@x.io'").rows == []


@pytest.mark.parametrize("seed_val", [1, 5, 13])
def test_index_results_identical_to_seqscan(db, seed_val):
    """Property: for random data/queries, index and seq-scan agree."""
    rng = random.Random(seed_val)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, k INT, s TEXT)")
    rows = [(i, rng.randint(0, 30), f"s{rng.randint(0, 30)}") for i in range(1, 200)]
    for r in rows:
        db.execute("INSERT INTO t VALUES (?, ?, ?)", r)
    # baseline (seq scan) answers
    baseline_eq = sorted(x[0] for x in db.execute("SELECT id FROM t WHERE k = 15").rows)
    baseline_rng = sorted(x[0] for x in db.execute("SELECT id FROM t WHERE k >= 10").rows)
    baseline_txt = sorted(x[0] for x in db.execute("SELECT id FROM t WHERE s = 's7'").rows)
    db.execute("CREATE INDEX idx_k ON t(k)")
    db.execute("CREATE INDEX idx_s ON t(s)")
    assert sorted(x[0] for x in db.execute("SELECT id FROM t WHERE k = 15").rows) == baseline_eq
    assert sorted(x[0] for x in db.execute("SELECT id FROM t WHERE k >= 10").rows) == baseline_rng
    assert sorted(x[0] for x in db.execute("SELECT id FROM t WHERE s = 's7'").rows) == baseline_txt
