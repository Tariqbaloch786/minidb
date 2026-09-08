# minidb architecture

This document walks the codebase bottom-up: from raw bytes on disk to a running
SQL statement. If you want to understand how a database actually works, reading
the files in this order is the intended tour.

## 1. The file format (`storage/pager.py`)

The entire database is one file split into fixed **4 KiB pages**. Page 0 is the
**meta page**, a small header:

```
magic "MDB1" | page_size | num_pages | catalog_root | free_list_head | next_txid
```

The `Pager` is the only thing that touches the file. It offers `read_page`,
`write_page`, `allocate_page` (reusing freed pages via a free list), and — the
important part — it **buffers writes**. Modified pages sit in memory in a dirty
set until `flush()` writes them out and `fsync`s. That buffering is what lets a
transaction stage many changes and commit (or discard) them atomically.

Reads are cached too, but reads and writes are tracked separately so a
read-only query never logs or flushes anything.

## 2. Durability: the write-ahead log (`storage/wal.py`)

minidb uses a **redo** WAL with a **WAL-first, force-at-commit** policy. Record
types are `PAGE` (an after-image), `COMMIT`, and `ABORT`; each record carries a
CRC32 so a torn write at the tail of the log is detected and ignored.

Commit sequence (see `Database._commit`):

1. For every dirty page, append a `PAGE` record with its after-image.
2. Append a `COMMIT` record and `fsync` the log.
3. Flush the dirty pages to the main file.
4. Truncate the WAL (checkpoint) — the main file is now authoritative.

Recovery (on open, `WAL.recover`):

1. Scan the log, collecting committed transaction ids and buffered page images.
2. Replay only the page images belonging to **committed** transactions.

This yields:

- **Durability** — a crash between steps 2 and 4 is repaired by replay.
- **Atomicity** — a crash before step 2 leaves an incomplete transaction whose
  pages have no `COMMIT` record, so they're skipped entirely.

Both directions are tested in `tests/test_transactions.py`.

## 3. The index: a persistent B+Tree (`storage/btree.py`)

Each table is stored as a B+Tree keyed by its `INT PRIMARY KEY`, with the whole
serialized row (a version chain — see §6) as the value. This is a **clustered**
layout: the table *is* its primary-key index.

- **Leaves** hold `key → value` entries and a pointer to the next leaf, so a
  full or range scan is a straight sequential walk.
- **Internal nodes** hold separator keys and child page ids.
- Nodes are one page each and **split by byte budget** (rather than a fixed
  fan-out), which keeps the code simple with variable-length values.

Supported operations: `get` (point lookup), `range(lo, hi)`, `items()` (ordered
full scan), `insert`/upsert, and `delete`. Leaves split on overflow; node
merging on delete is left as future work (leaves may become sparse, which is
correct, just not optimally compact).

## 4. SQL front-end (`sql/`)

- `tokenizer.py` — a hand-written lexer producing `Token`s (keywords,
  identifiers, numbers, single-quoted strings with `''` escaping, symbols).
- `ast.py` — plain dataclasses for each statement and expression node.
- `parser.py` — a recursive-descent parser. Expression precedence is
  `OR` < `AND` < `NOT` < comparison, with parentheses.

No parser generators, no regex soup — just the classic textbook approach so the
grammar is easy to read and extend.

## 5. Catalog, types, planner, executor (`engine/`)

- **Catalog** (`catalog.py`) — the map from table name to schema (columns,
  types, primary key, B+Tree root page). It's serialized to JSON and stored in a
  page chain *inside the database file*, so schema changes are covered by the
  same WAL/rollback guarantees as user data.
- **Types** (`types.py`) — compact binary row encoding: a presence byte plus a
  typed payload per column (`q` for INT, `d` for FLOAT, length-prefixed UTF-8
  for TEXT).
- **Planner** (`planner.py`) — the one real optimization. It flattens the
  `WHERE` clause across `AND`, extracts primary-key bounds, and chooses:
  - `IndexSeek` for `pk = k`,
  - `IndexRange` for `pk` inequalities,
  - `SeqScan` otherwise.
  Whatever it can't turn into a bound becomes a **residual filter** applied per
  row. `EXPLAIN` prints the result.
- **Executor** (`executor.py`) — the glue. It asks the planner for an access
  path, walks the B+Tree, filters candidate rows through MVCC visibility and the
  residual predicate (with three-valued logic), and writes new row versions on
  `INSERT` / `UPDATE` / `DELETE`.

## 6. Transactions and MVCC (`txn/mvcc.py`)

Instead of a single value, each key maps to a **version chain**: a list of
`Version(xmin, xmax, deleted, data)`.

- `INSERT` appends a version with `xmin = my txid`.
- `UPDATE` sets `xmax = my txid` on the currently visible version and appends a
  new one.
- `DELETE` sets `xmax` and appends a tombstone.

A transaction takes a **snapshot** at `BEGIN`: the next-txid boundary plus the
set of transactions active at that moment. A version is visible if its creator
committed before the snapshot (and isn't the snapshot's own in-flight set), and
it hasn't been retired by another such transaction. Your own writes are always
visible to you.

Because the durability model only ever flushes *committed* transactions'
pages, any version found on disk after a restart was necessarily written by a
committed transaction — so an `xmin` we've never heard of is, by construction,
committed. That's why minidb needs no persistent commit log.

`ROLLBACK` is almost free: the transaction's page changes were only ever in the
pager's buffer, so discarding them (and reloading the in-memory catalog) undoes
everything.

**Concurrency scope.** This build serializes writers (one active writer at a
time). The version chains and visibility rules are the genuine article; a
lock/latch manager for true multi-writer concurrency is the next milestone.

## 7. Putting it together (`database.py`)

`Database.execute(sql)` parses one statement and:

- routes `BEGIN` / `COMMIT` / `ROLLBACK` to the transaction lifecycle, or
- runs DDL/DML/SELECT inside the current explicit transaction, or
- wraps a lone statement in an **autocommit** transaction that commits on
  success and rolls back on error.

`repl.py` is a thin `sqlite3`-style shell on top, with `.tables`, `.schema`, and
timing output.
