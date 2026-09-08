"""Aggregation, GROUP BY / HAVING, and parameterized-query tests."""

import pytest

from minidb.engine.executor import ExecutionError


def setup_sales(db):
    db.execute("CREATE TABLE sales (id INT PRIMARY KEY, region TEXT, amount INT)")
    db.execute(
        "INSERT INTO sales VALUES "
        "(1, 'west', 100), (2, 'west', 200), (3, 'east', 50), "
        "(4, 'east', 75), (5, 'east', NULL)"
    )


def test_count_star(db):
    setup_sales(db)
    assert db.execute("SELECT COUNT(*) FROM sales").rows == [[5]]


def test_count_ignores_nulls(db):
    setup_sales(db)
    # COUNT(col) counts non-NULL values only
    assert db.execute("SELECT COUNT(amount) FROM sales").rows == [[4]]


def test_sum_avg_min_max(db):
    setup_sales(db)
    r = db.execute("SELECT SUM(amount), AVG(amount), MIN(amount), MAX(amount) FROM sales")
    assert r.columns == ["sum(amount)", "avg(amount)", "min(amount)", "max(amount)"]
    assert r.rows == [[425, 106.25, 50, 200]]


def test_aggregate_over_empty_table_is_null_or_zero(db):
    db.execute("CREATE TABLE e (id INT PRIMARY KEY, v INT)")
    r = db.execute("SELECT COUNT(*), SUM(v), AVG(v) FROM e")
    assert r.rows == [[0, None, None]]


def test_group_by(db):
    setup_sales(db)
    r = db.execute(
        "SELECT region, COUNT(*), SUM(amount) FROM sales GROUP BY region ORDER BY region"
    )
    assert r.rows == [["east", 3, 125], ["west", 2, 300]]


def test_having(db):
    setup_sales(db)
    r = db.execute(
        "SELECT region, SUM(amount) FROM sales GROUP BY region HAVING SUM(amount) > 150"
    )
    assert r.rows == [["west", 300]]


def test_order_by_aggregate(db):
    setup_sales(db)
    r = db.execute(
        "SELECT region, COUNT(*) FROM sales GROUP BY region ORDER BY COUNT(*) DESC"
    )
    assert r.rows == [["east", 3], ["west", 2]]


def test_non_grouped_column_rejected(db):
    setup_sales(db)
    with pytest.raises(ExecutionError):
        db.execute("SELECT region, id FROM sales GROUP BY region")


def test_aggregate_in_where_rejected(db):
    setup_sales(db)
    with pytest.raises(ExecutionError):
        db.execute("SELECT region FROM sales WHERE SUM(amount) > 1 GROUP BY region")


def test_star_aggregate_argument_rejected(db):
    with pytest.raises(Exception):
        db.execute("CREATE TABLE t (id INT PRIMARY KEY, v INT)")
        db.execute("SELECT SUM(*) FROM t")


# -- parameterized queries -------------------------------------------------
def test_parameters_bind_positionally(db):
    setup_sales(db)
    r = db.execute(
        "SELECT id FROM sales WHERE region = ? AND amount >= ? ORDER BY id",
        ("west", 150),
    )
    assert r.rows == [[2]]


def test_parameters_in_insert(db):
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO t VALUES (?, ?)", (1, "ada"))
    assert db.execute("SELECT name FROM t WHERE id = ?", (1,)).rows == [["ada"]]


def test_parameter_count_mismatch(db):
    setup_sales(db)
    with pytest.raises(Exception):
        db.execute("SELECT * FROM sales WHERE region = ?", ("west", "extra"))


def test_parameter_is_data_not_sql(db):
    """A classic injection string passed as a parameter is inert."""
    setup_sales(db)
    evil = "west'; DROP TABLE sales;--"
    assert db.execute("SELECT id FROM sales WHERE region = ?", (evil,)).rows == []
    # the table is untouched
    assert db.execute("SELECT COUNT(*) FROM sales").rows == [[5]]
