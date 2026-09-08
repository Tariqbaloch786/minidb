"""Tests for the PEP 249 (DB-API 2.0) driver."""

import pytest

import minidb.dbapi as dbapi


def test_module_globals():
    assert dbapi.apilevel == "2.0"
    assert dbapi.paramstyle == "qmark"
    assert isinstance(dbapi.threadsafety, int)


def test_basic_cursor_flow():
    conn = dbapi.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    cur.executemany("INSERT INTO t VALUES (?, ?)", [(1, "ada"), (2, "grace")])
    conn.commit()

    cur.execute("SELECT id, name FROM t ORDER BY id")
    assert [d[0] for d in cur.description] == ["id", "name"]
    assert cur.fetchone() == (1, "ada")
    assert cur.fetchall() == [(2, "grace")]
    assert cur.fetchone() is None
    conn.close()


def test_rowcount_for_dml():
    conn = dbapi.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    cur.executemany("INSERT INTO t VALUES (?, ?)", [(1, 1), (2, 2), (3, 3)])
    cur.execute("UPDATE t SET v = 0 WHERE id >= ?", (2,))
    assert cur.rowcount == 2
    conn.close()


def test_iteration():
    conn = dbapi.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    cur.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
    conn.commit()
    cur.execute("SELECT id FROM t ORDER BY id")
    assert [row[0] for row in cur] == [1, 2, 3]
    conn.close()


def test_rollback():
    conn = dbapi.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    conn.commit()
    cur.execute("INSERT INTO t VALUES (1)")
    conn.rollback()
    cur.execute("SELECT COUNT(*) FROM t")
    assert cur.fetchone() == (0,)
    conn.close()


def test_integrity_error_on_duplicate_key():
    conn = dbapi.connect(":memory:")
    cur = conn.cursor()
    cur.execute("CREATE TABLE t (id INT PRIMARY KEY)")
    cur.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(dbapi.IntegrityError):
        cur.execute("INSERT INTO t VALUES (1)")
    conn.close()


def test_programming_error_on_bad_sql():
    conn = dbapi.connect(":memory:")
    cur = conn.cursor()
    with pytest.raises(dbapi.ProgrammingError):
        cur.execute("SELECT FROM WHERE")
    conn.close()


def test_context_manager_commits(tmp_path):
    path = str(tmp_path / "d.db")
    with dbapi.connect(path) as conn:
        conn.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t VALUES (?, ?)", (1, "kept"))
    # a fresh connection sees the committed row
    with dbapi.connect(path) as conn2:
        cur = conn2.cursor()
        cur.execute("SELECT v FROM t")
        assert cur.fetchall() == [("kept",)]
