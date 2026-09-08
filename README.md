# minidb

**A small but *real* relational database engine, written from scratch in pure Python — no `sqlite3`, no ORM, no dependencies.**

[![CI](https://github.com/Tariqbaloch786/minidb/actions/workflows/ci.yml/badge.svg)](https://github.com/Tariqbaloch786/minidb/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-39%20passing-brightgreen)
![Dependencies](https://img.shields.io/badge/dependencies-0-lightgrey)

minidb implements the pieces a database course spends a semester on — a paged
storage engine, a **B+Tree** index, a **write-ahead log with crash recovery**, a
hand-written **SQL parser**, a **query planner**, and **MVCC transactions** with
`BEGIN` / `COMMIT` / `ROLLBACK` — in ~2,000 lines of dependency-free, tested
Python.

```text
minidb> CREATE TABLE users (id INT PRIMARY KEY, name TEXT NOT NULL, age INT);
minidb> INSERT INTO users VALUES (1, 'Ada', 36), (2, 'Grace', 41);
minidb> BEGIN;
   ...> UPDATE users SET age = 42 WHERE id = 2;
   ...> ROLLBACK;                       -- the change never happened
minidb> EXPLAIN SELECT * FROM users WHERE id = 1;
QUERY PLAN
-----------------------------------------
Index Seek on users_pkey (id = 1)
(1 row, 0.20ms)
```

---

## Why this project exists

Most "database" projects on GitHub are a schema and an ORM. This one is the
engine *underneath* an ORM. It's built to answer questions like:

- How does a database survive `kill -9` in the middle of a write? *(write-ahead log + redo recovery)*
- Why is `WHERE id = 5` fast but `WHERE name = 'x'` slow? *(B+Tree index seek vs. sequential scan — the planner picks)*
- What actually happens on `ROLLBACK`? *(buffered pages are discarded; committed data is never touched)*
- How can two transactions read the same row and see different values? *(MVCC version chains + snapshot visibility)*

Every one of those is implemented here and covered by a test.

## Architecture

```text
                 ┌──────────────────────────────────────────────┐
   SQL text ───▶ │  sql/   tokenizer → parser → AST             │
                 └──────────────────────────────────────────────┘
                                    │  statement
                                    ▼
                 ┌──────────────────────────────────────────────┐
                 │  engine/ planner (access path) → executor     │
                 │          catalog (schemas)   types (rows)     │
                 └──────────────────────────────────────────────┘
                          │ read/write versioned rows
                          ▼
                 ┌──────────────────────────────────────────────┐
                 │  txn/  MVCC: version chains + snapshot rules   │
                 └──────────────────────────────────────────────┘
                          │ get/put(key, bytes)
                          ▼
                 ┌──────────────────────────────────────────────┐
                 │  storage/ B+Tree  →  Pager  →  single file     │
                 │           WAL (redo log + crash recovery)      │
                 └──────────────────────────────────────────────┘
```

The whole database — user rows, indexes, *and* the system catalog — lives in a
single file divided into 4 KiB pages, exactly like SQLite or Postgres. A second
`.wal` file guarantees durability and atomicity.

| Layer | File | Responsibility |
|-------|------|----------------|
| Pager | [`storage/pager.py`](minidb/storage/pager.py) | Fixed-size page I/O, allocation, free list, dirty-page buffering |
| WAL | [`storage/wal.py`](minidb/storage/wal.py) | Redo logging, `fsync` on commit, crash recovery, CRC-checked records |
| B+Tree | [`storage/btree.py`](minidb/storage/btree.py) | Ordered `int → bytes` index; point, range and full scans; node splits |
| Tokenizer/Parser | [`sql/`](minidb/sql/) | Hand-written lexer + recursive-descent parser → typed AST |
| Catalog | [`engine/catalog.py`](minidb/engine/catalog.py) | Table schemas, persisted inside the DB file |
| Planner | [`engine/planner.py`](minidb/engine/planner.py) | Turns `WHERE` into an index seek / range scan / seq scan + residual filter |
| Executor | [`engine/executor.py`](minidb/engine/executor.py) | Runs statements, evaluates predicates (3-valued logic), writes row versions |
| MVCC | [`txn/mvcc.py`](minidb/txn/mvcc.py) | Version chains, `xmin`/`xmax` visibility, snapshot isolation, vacuum |
| Facade | [`database.py`](minidb/database.py) | `Database.execute(sql)`, transaction lifecycle, commit/rollback |

There's a fuller write-up in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quickstart

No install needed — there are zero dependencies.

```bash
git clone https://github.com/Tariqbaloch786/minidb.git
cd minidb

# interactive shell (in-memory, or pass a file to persist)
python -m minidb
python -m minidb mydata.db

# run the guided demo
python examples/demo.py
```

Or use it as a library:

```python
from minidb import Database

db = Database("app.db")          # or ":memory:"
db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
db.execute("INSERT INTO t VALUES (1, 'ada'), (2, 'grace')")

rows = db.execute("SELECT name FROM t WHERE id = 2").rows
print(rows)                       # [['grace']]

with_txn = db.execute("BEGIN")
db.execute("DELETE FROM t WHERE id = 1")
db.execute("ROLLBACK")            # id=1 is back
db.close()
```

## Installed as a package

```bash
pip install -e .
minidb mydata.db      # console entry point
```

## Supported SQL

```sql
CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL, score FLOAT);
DROP TABLE t;

INSERT INTO t [(cols...)] VALUES (...), (...);

SELECT * | col, ...
    FROM t
    [WHERE <predicate>]
    [ORDER BY col [ASC|DESC]]
    [LIMIT n];

UPDATE t SET col = val, ... [WHERE <predicate>];
DELETE FROM t [WHERE <predicate>];

BEGIN;  COMMIT;  ROLLBACK;
EXPLAIN SELECT ...;              -- show the chosen access path
```

- **Types:** `INT`, `FLOAT`, `TEXT`, with `NULL`.
- **Predicates:** `=  !=  <  <=  >  >=`, combined with `AND` / `OR` / `NOT` and parentheses, evaluated with proper SQL three-valued logic (a comparison against `NULL` is *unknown*, not false).
- **Constraints:** exactly one `INT PRIMARY KEY` (it *is* the clustered B+Tree key), plus `NOT NULL`.

## The query planner in action

```sql
EXPLAIN SELECT * FROM users WHERE id = 42;
-- Index Seek on users_pkey (id = 42)

EXPLAIN SELECT * FROM users WHERE id >= 10 AND id <= 20;
-- Index Range Scan on users_pkey (10 <= id <= 20)

EXPLAIN SELECT * FROM users WHERE name = 'ada';
-- Seq Scan on users  [filter]
```

The planner splits a `WHERE` clause into conjuncts, pushes any primary-key
bounds down into a B+Tree seek or range scan, and keeps the rest as a residual
filter. That single optimization is the difference between the two numbers
below.

## Benchmarks

Pure-Python and unapologetically so — the point is *shape*, not raw speed.
Measured on a laptop, 20,000 rows (`python benchmarks/bench.py --disk`):

| Operation | Throughput / latency |
|-----------|----------------------|
| Bulk insert (one transaction) | ~6,700 rows/sec |
| Primary-key point lookups (index seek) | ~12,000 lookups/sec |
| Range scan, 100 rows via index | **0.9 ms** |
| Full sequential scan + filter (20k rows) | 94 ms |

The last two rows are the same data: an index range scan is ~**100×** faster
than the sequential scan the planner falls back to when it can't use the key.

## How durability & atomicity work

minidb uses a **WAL-first, force-at-commit** policy:

1. A transaction's page changes stay in an in-memory buffer.
2. On `COMMIT`, every modified page's after-image is appended to the WAL, a
   `COMMIT` record is written and **`fsync`'d**, and only then are the pages
   flushed to the main file and the WAL checkpointed.

That gives two guarantees, both directly tested:

- **Crash *after* the commit record** → recovery replays the logged pages, so
  committed data survives even if the main-file write never happened. *(durability)*
- **Crash *before* the commit record** → the transaction's pages are ignored on
  replay and were never in the main file. *(atomicity)*

See [`test_transactions.py`](tests/test_transactions.py) — the recovery tests
literally build a torn-write scenario and reopen the database.

## MVCC in one paragraph

Every row is a **version chain**. A write doesn't overwrite — it appends a new
version stamped with the creating transaction id (`xmin`) and retires the old
one with `xmax`. Each transaction reads through a **snapshot** taken when it
began, so it sees a consistent point-in-time view and never another
transaction's uncommitted work. Because only committed transactions' pages ever
reach disk, any version found after a restart is — by construction — committed,
which is why no persistent commit log is needed. *(This build runs a single
active writer at a time; the visibility machinery is the real thing and
multi-writer concurrency is the documented next step.)*

## Testing

```bash
pip install -e ".[dev]"
pytest                    # 39 tests across every layer
ruff check .              # lint
```

The suite covers the B+Tree (including multi-level splits and reopen), the
tokenizer/parser, end-to-end SQL and constraints, the planner's access-path
choices, transaction rollback, durability across reopen, MVCC visibility, and
both crash-recovery directions. CI runs it on Linux and Windows across Python
3.10–3.12.

## Project layout

```text
minidb/
  storage/   pager.py  wal.py  btree.py
  sql/       tokenizer.py  ast.py  parser.py
  engine/    catalog.py  types.py  planner.py  executor.py
  txn/       mvcc.py
  database.py  repl.py  __main__.py
tests/       test_btree.py  test_parser.py  test_sql.py  test_transactions.py
benchmarks/  bench.py
examples/    demo.py  tour.sql
docs/        ARCHITECTURE.md
```

## Roadmap

Deliberately out of scope for v0.1, and each a fun next step:

- [ ] Secondary indexes (right now the primary key is the only index)
- [ ] Multi-writer concurrency with lock/latch management
- [ ] Joins and aggregation (`GROUP BY`, `COUNT`, `SUM`)
- [ ] B+Tree node merging on delete (leaves currently only split)
- [ ] Overflow pages for values larger than one page
- [ ] A cost-based planner using table statistics

## License

MIT — see [LICENSE](LICENSE).
