-- A guided tour of minidb. Run it with:
--   python -m minidb demo.db < examples/tour.sql
-- or paste the statements into the REPL.

CREATE TABLE employees (
    id       INT PRIMARY KEY,
    name     TEXT NOT NULL,
    dept     TEXT,
    salary   INT
);

INSERT INTO employees VALUES
    (1, 'Ada Lovelace',   'Engineering', 165000),
    (2, 'Grace Hopper',   'Engineering', 172000),
    (3, 'Linus Torvalds', 'Kernel',      158000),
    (4, 'Margaret Hamilton', 'Apollo',   180000);

-- Point lookup -> the planner uses an Index Seek on the primary key.
EXPLAIN SELECT * FROM employees WHERE id = 3;
SELECT name, dept FROM employees WHERE id = 3;

-- Range predicate on the key -> Index Range Scan.
EXPLAIN SELECT * FROM employees WHERE id >= 2 AND id <= 3;

-- Filter on a non-key column -> Seq Scan with a residual filter.
SELECT name, salary FROM employees WHERE salary > 160000 ORDER BY salary DESC;

-- A secondary index turns that non-key filter into an index scan.
CREATE INDEX idx_emp_name ON employees(name);
EXPLAIN SELECT * FROM employees WHERE name = 'Grace';
SELECT id, dept FROM employees WHERE name = 'Grace';

-- Joins. The ON clause is an equi-join on the inner table's primary key, so
-- the planner uses an index nested-loop join (a PK seek per outer row).
CREATE TABLE projects (id INT PRIMARY KEY, owner_id INT, title TEXT);
INSERT INTO projects VALUES (100, 1, 'Analytical Engine'), (101, 4, 'AGC');
EXPLAIN SELECT * FROM projects JOIN employees ON projects.owner_id = employees.id;
SELECT employees.name, projects.title
    FROM projects JOIN employees ON projects.owner_id = employees.id
    ORDER BY projects.id;

-- LEFT JOIN keeps employees with no project (title comes back NULL).
SELECT employees.name, projects.title
    FROM employees LEFT JOIN projects ON projects.owner_id = employees.id
    ORDER BY employees.id;

-- Transactions: this change is rolled back and never persists.
BEGIN;
UPDATE employees SET salary = 0 WHERE dept = 'Engineering';
SELECT name, salary FROM employees WHERE dept = 'Engineering';
ROLLBACK;

-- After rollback the original salaries are intact.
SELECT name, salary FROM employees ORDER BY id;
