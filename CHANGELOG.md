# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [0.2.0]

Made minidb usable by real applications, three ways.

### Added
- **Parameterized queries** — `?` placeholders bound on the AST after parsing,
  so user data never becomes part of the SQL text (SQL-injection safe).
- **Aggregation** — `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` with `GROUP BY`,
  `HAVING`, and `ORDER BY` over aggregates; SQL `NULL` semantics.
- **DB-API 2.0 driver** (`minidb.dbapi`) — a PEP 249 interface (`connect`,
  cursors, `fetch*`, `executemany`, `description`, `rowcount`, exception
  hierarchy, context managers) so minidb works like `sqlite3` for applications.
- **Client/server mode** (`minidb.server` + `minidb.client`) — a threaded TCP
  server (newline-JSON protocol) letting many clients share one database, with
  serialized access and per-connection transactions. New `minidb-server`
  console script.

### Notes
- 79 tests (up from 52); documentation repositions minidb as a SQLite-class
  engine with its concurrency limits stated plainly.

## [0.1.0]

Initial release: the storage and query engine.

### Added
- Single-file **paged storage** with a free list.
- **Write-ahead log** with `fsync`-on-commit durability and crash recovery
  (redo of committed transactions; atomicity for uncommitted ones).
- Persistent **B+Tree** index: point, range, and ordered full scans; node splits.
- **SQL** front-end: tokenizer, recursive-descent parser, typed AST.
- **Query planner** choosing index seek / range scan / sequential scan.
- **`INNER` / `LEFT` joins** with an index-nested-loop strategy.
- **MVCC** snapshot-isolation transactions with `BEGIN` / `COMMIT` / `ROLLBACK`.
- A `sqlite3`-style REPL, benchmarks, and a test suite.
