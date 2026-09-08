"""End-to-end tests for JOIN support."""

import pytest

from minidb.engine.executor import ExecutionError


def setup_shop(db):
    db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total INT)")
    db.execute("INSERT INTO users VALUES (1, 'ada'), (2, 'grace'), (3, 'linus')")
    db.execute(
        "INSERT INTO orders VALUES "
        "(10, 1, 100), (11, 1, 250), (12, 2, 70), (13, 99, 5)"
    )


def test_inner_join_basic(db):
    setup_shop(db)
    r = db.execute(
        "SELECT users.name, orders.total FROM orders "
        "JOIN users ON orders.user_id = users.id "
        "ORDER BY orders.total"
    )
    assert r.columns == ["name", "total"]
    assert r.rows == [["grace", 70], ["ada", 100], ["ada", 250]]


def test_inner_join_drops_unmatched(db):
    setup_shop(db)
    # order 13 has user_id 99 (no such user) -> excluded by inner join
    r = db.execute(
        "SELECT orders.id FROM orders JOIN users ON orders.user_id = users.id"
    )
    assert sorted(x[0] for x in r.rows) == [10, 11, 12]


def test_left_join_keeps_unmatched_with_nulls(db):
    setup_shop(db)
    r = db.execute(
        "SELECT orders.id, users.name FROM orders "
        "LEFT JOIN users ON orders.user_id = users.id "
        "ORDER BY orders.id"
    )
    assert r.rows == [[10, "ada"], [11, "ada"], [12, "grace"], [13, None]]


def test_left_join_users_with_no_orders(db):
    setup_shop(db)
    # user 3 (linus) placed no orders -> appears once with NULL order total
    r = db.execute(
        "SELECT users.name, orders.total FROM users "
        "LEFT JOIN orders ON orders.user_id = users.id "
        "ORDER BY users.name"
    )
    assert ["linus", None] in r.rows
    # ada has two orders -> two rows
    assert sum(1 for row in r.rows if row[0] == "ada") == 2


def test_join_star_projection(db):
    setup_shop(db)
    r = db.execute(
        "SELECT * FROM users JOIN orders ON users.id = orders.user_id "
        "WHERE users.id = 1 ORDER BY orders.id"
    )
    # ambiguous 'id' becomes qualified; non-ambiguous names stay bare
    assert r.columns == ["users.id", "name", "orders.id", "user_id", "total"]
    assert r.rows[0] == [1, "ada", 10, 1, 100]


def test_table_star_projection(db):
    setup_shop(db)
    r = db.execute(
        "SELECT users.* FROM users JOIN orders ON users.id = orders.user_id "
        "WHERE orders.id = 12"
    )
    assert r.columns == ["users.id", "name"]  # note: 'id' is ambiguous across the join
    assert r.rows == [[2, "grace"]]


def test_ambiguous_column_is_rejected(db):
    setup_shop(db)
    with pytest.raises(ExecutionError):
        db.execute("SELECT id FROM users JOIN orders ON users.id = orders.user_id")


def test_where_filters_across_join(db):
    setup_shop(db)
    r = db.execute(
        "SELECT users.name, orders.total FROM users "
        "JOIN orders ON users.id = orders.user_id "
        "WHERE orders.total >= 100 ORDER BY orders.total DESC"
    )
    assert r.rows == [["ada", 250], ["ada", 100]]


def test_three_way_join(db):
    db.execute("CREATE TABLE a (id INT PRIMARY KEY, b_id INT)")
    db.execute("CREATE TABLE b (id INT PRIMARY KEY, c_id INT)")
    db.execute("CREATE TABLE c (id INT PRIMARY KEY, label TEXT)")
    db.execute("INSERT INTO a VALUES (1, 10), (2, 20)")
    db.execute("INSERT INTO b VALUES (10, 100), (20, 200)")
    db.execute("INSERT INTO c VALUES (100, 'x'), (200, 'y')")
    r = db.execute(
        "SELECT a.id, c.label FROM a "
        "JOIN b ON a.b_id = b.id "
        "JOIN c ON b.c_id = c.id "
        "ORDER BY a.id"
    )
    assert r.rows == [[1, "x"], [2, "y"]]


def test_explain_join_uses_index_seek(db):
    setup_shop(db)
    plan = db.execute(
        "EXPLAIN SELECT * FROM orders JOIN users ON orders.user_id = users.id"
    ).rows
    text = "\n".join(row[0] for row in plan)
    assert "Nested Loop" in text
    assert "Index Seek on users_pkey" in text  # inner side driven by PK


def test_unknown_table_qualifier(db):
    setup_shop(db)
    with pytest.raises(ExecutionError):
        db.execute("SELECT ghost.name FROM users JOIN orders ON users.id = orders.user_id")
