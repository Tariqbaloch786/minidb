"""End-to-end SQL tests: CRUD, constraints, planner, projection."""

import pytest

from minidb.engine.executor import ExecutionError


def setup_users(db):
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT)")
    db.execute(
        "INSERT INTO users VALUES (1, 'ada', 36), (2, 'linus', 54), (3, 'grace', 41)"
    )


def test_create_insert_select(db):
    setup_users(db)
    r = db.execute("SELECT * FROM users ORDER BY id")
    assert r.columns == ["id", "name", "age"]
    assert r.rows == [[1, "ada", 36], [2, "linus", 54], [3, "grace", 41]]


def test_projection(db):
    setup_users(db)
    r = db.execute("SELECT name FROM users WHERE id = 2")
    assert r.columns == ["name"]
    assert r.rows == [["linus"]]


def test_where_and_order_and_limit(db):
    setup_users(db)
    r = db.execute("SELECT name FROM users WHERE age > 40 ORDER BY age DESC LIMIT 1")
    assert r.rows == [["linus"]]


def test_update(db):
    setup_users(db)
    n = db.execute("UPDATE users SET age = 37 WHERE id = 1").rowcount
    assert n == 1
    assert db.execute("SELECT age FROM users WHERE id = 1").rows == [[37]]


def test_delete(db):
    setup_users(db)
    n = db.execute("DELETE FROM users WHERE id = 2").rowcount
    assert n == 1
    assert [r[0] for r in db.execute("SELECT id FROM users").rows] == [1, 3]


def test_duplicate_primary_key(db):
    setup_users(db)
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO users VALUES (1, 'dup', 1)")


def test_not_null_constraint(db):
    setup_users(db)
    with pytest.raises(ExecutionError):
        db.execute("INSERT INTO users VALUES (9, NULL, 1)")


def test_type_checking(db):
    setup_users(db)
    with pytest.raises(Exception):
        db.execute("INSERT INTO users VALUES (9, 'x', 'not-an-int')")


def test_cannot_update_primary_key(db):
    setup_users(db)
    with pytest.raises(ExecutionError):
        db.execute("UPDATE users SET id = 5 WHERE id = 1")


def test_no_such_table(db):
    with pytest.raises(ExecutionError):
        db.execute("SELECT * FROM ghost")


def test_drop_table(db):
    setup_users(db)
    db.execute("DROP TABLE users")
    with pytest.raises(ExecutionError):
        db.execute("SELECT * FROM users")


def test_null_three_valued_logic(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
    db.execute("INSERT INTO t VALUES (1, 10), (2, NULL), (3, 30)")
    # NULL rows are filtered out of comparisons (unknown != true)
    r = db.execute("SELECT id FROM t WHERE v > 5 ORDER BY id")
    assert [x[0] for x in r.rows] == [1, 3]


@pytest.mark.parametrize(
    "where,method",
    [
        ("WHERE id = 5", "Index Seek"),
        ("WHERE id > 5 AND id < 10", "Index Range"),
        ("WHERE name = 'x'", "Seq Scan"),
        ("", "Seq Scan"),
    ],
)
def test_planner_chooses_access_path(db, where, method):
    setup_users(db)
    plan = db.execute(f"EXPLAIN SELECT * FROM users {where}").rows[0][0]
    assert method in plan
