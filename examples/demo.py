"""A runnable tour of minidb's features. `python examples/demo.py`."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from minidb import Database


def show(db, sql):
    result = db.execute(sql)
    print(f"\n>>> {sql}")
    if result.kind in ("select", "explain"):
        print("   ", result.columns)
        for row in result.rows:
            print("   ", row)
    else:
        print("   ", result.message)


def main():
    db = Database(":memory:")
    show(db, "CREATE TABLE employees (id INT PRIMARY KEY, name TEXT NOT NULL, dept TEXT, salary INT)")
    show(db, "INSERT INTO employees VALUES "
             "(1, 'Ada', 'Eng', 165000), (2, 'Grace', 'Eng', 172000), "
             "(3, 'Linus', 'Kernel', 158000)")

    show(db, "EXPLAIN SELECT * FROM employees WHERE id = 2")
    show(db, "SELECT name, salary FROM employees WHERE salary > 160000 ORDER BY salary DESC")

    print("\n--- joins ---")
    show(db, "CREATE TABLE orders (id INT PRIMARY KEY, emp_id INT, amount INT)")
    show(db, "INSERT INTO orders VALUES (10, 1, 500), (11, 1, 300), (12, 2, 900)")
    show(db, "EXPLAIN SELECT * FROM orders JOIN employees ON orders.emp_id = employees.id")
    show(db, "SELECT employees.name, orders.amount FROM employees "
             "JOIN orders ON orders.emp_id = employees.id ORDER BY orders.amount DESC")
    show(db, "SELECT employees.name, orders.amount FROM employees "
             "LEFT JOIN orders ON orders.emp_id = employees.id ORDER BY employees.id")

    print("\n--- transaction that gets rolled back ---")
    show(db, "BEGIN")
    show(db, "UPDATE employees SET salary = 0 WHERE dept = 'Eng'")
    show(db, "SELECT name, salary FROM employees WHERE dept = 'Eng'")
    show(db, "ROLLBACK")
    show(db, "SELECT name, salary FROM employees ORDER BY id")

    db.close()


if __name__ == "__main__":
    main()
