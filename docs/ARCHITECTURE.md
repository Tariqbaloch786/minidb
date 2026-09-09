# minidb architecture

This document walks the codebase bottom-up: from raw bytes on disk to a running
SQL statement. If you want to understand how a database actually works, reading
the files in this order is the intended tour.

## 1. The file format (`storage/pager.py`)

The entire database is one file split into fixed **4 KiB physical pages**. Every
physical page begins with an 8-byte header — a **CRC32** and the **page id** —
followed by `DATA_SIZE` (4088) bytes of *data* that the layers above see:

```
[ crc32 (u32) | page_id (u32) | data (4088 bytes) ]
```

Page 0's data is the **meta page**:

```
magic "MDB2" | page_size | num_pages | catalog_root | free_list_head | next_txid
```

The `Pager` is the only thing that touches the file, and it does **immediate,
checksummed** physical I/O: `write_page` stamps the CRC (over `page_id || data`),
and `read_page` recomputes it and checks the page id, raising **`CorruptionError`**
on any mismatch, short read (truncation), or misdirected page. It does *no*
caching or write-buffering itself — that is the buffer pool's job (§10). The
`MDB1`→`MDB2` magic bump reflects the added header; older files are not readable.

Checksums are an *integrity* check, not a MAC and not a repair mechanism: they
turn silent corruption into a deterministic error. WAL redo (§2) can heal a
damaged page when a committed after-image for it is still in the log.

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
full scan), `insert`/upsert, and `delete`. Leaves split on overflow; **delete
rebalances** (merge/redistribute with a sibling, root collapse) and frees empty
pages.

**Large values (overflow pages).** A value bigger than half a page is written to
a linked chain of *overflow pages*, and the leaf keeps only an 8-byte pointer
(head page id + total length), flagged by the high bit of the entry's length
field — so old files, which never set that bit, stay readable. `get`/scans
reassemble the value transparently; overwriting or deleting frees the old chain,
and `free_all` (drop table/index) reclaims chains too. Values from a few bytes
to many megabytes are supported. A `validate()` method asserts the structural invariants (key order,
balance, sibling-chain consistency) and is exercised by randomized fuzz tests.
The tree is **key-codec generic**: primary keys use fixed-width `int` keys and
secondary indexes use variable-width `bytes` keys, over the same code.

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

### Joins

`SELECT` may join several tables. The executor builds each intermediate row as a
**namespace** keyed by qualified `table.col` names (plus bare `col` names when
they're unambiguous across the join, so unqualified references still work and
ambiguous ones raise a clear error). Every column reference is validated up
front against that namespace.

Joins run as **nested loops**, left to right. Two optimizations make them more
than a naive cartesian product:

1. **Predicate pushdown** — `WHERE` conjuncts that mention only the base table
   are handed to the base table's access path, so an indexable base filter still
   becomes a seek.
2. **Index nested-loop join** — when a join's `ON` clause is an equi-join
   against the inner table's primary key (`outer.x = inner.pk`), the inner side
   is a B+Tree **seek per outer row** instead of a full scan. `EXPLAIN` labels
   this `Index Seek on <inner>_pkey ... [per outer row]`.

`LEFT JOIN` emits the outer row with the inner columns set to `NULL` when no
inner row matches. `RIGHT` / `FULL` joins and a hash-join strategy for non-PK
equi-joins are future work.

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

`ROLLBACK` is almost free: the transaction's page changes were only ever dirty
frames in the buffer pool (never written to disk, by the no-steal policy — §10),
so discarding them (and reloading the in-memory catalog) undoes everything.

**Concurrency scope.** This build serializes writers (one active writer at a
time). The version chains and visibility rules are the genuine article; a
lock/latch manager for true multi-writer concurrency is the next milestone.

## 7. Putting it together (`database.py`)

`Database.execute(sql)` parses one statement and:

- routes `BEGIN` / `COMMIT` / `ROLLBACK` to the transaction lifecycle, or
- runs DDL/DML/SELECT inside the current explicit transaction, or
- wraps a lone statement in an **autocommit** transaction that commits on
  success and rolls back on error.

`Database.execute` also takes `params`: after parsing, `?` placeholders are
replaced with the supplied values (`sql/parser.py::bind_parameters`). Binding
happens on the AST, never by string-splicing, so parameter values can't change
the shape of a query — that's the injection-safety guarantee.

`repl.py` is a thin `sqlite3`-style shell on top, with `.tables`, `.schema`, and
timing output.

## 8. Aggregation and grouping (`engine/executor.py`)

A query aggregates when it has `GROUP BY`, `HAVING`, or an aggregate in the
`SELECT` list. The executor first produces the filtered (and joined) rows exactly
as for a normal query, then:

1. partitions rows into groups by the tuple of `GROUP BY` values (a hash group,
   first-seen order preserved);
2. computes each needed aggregate per group (`COUNT/SUM/AVG/MIN/MAX`, with SQL
   `NULL` handling — non-`*` aggregates skip `NULL`s);
3. builds a per-group namespace holding the group-by values *and* the aggregate
   results (keyed by a canonical label like `sum(amount)`), so `HAVING`,
   `ORDER BY`, and the projection can all reference either.

The usual SQL rule is enforced: a non-aggregated `SELECT`/`HAVING` column must
appear in `GROUP BY`, and aggregates are rejected in `WHERE` (that's what
`HAVING` is for).

## 9. Application interfaces (`dbapi.py`, `server.py`, `client.py`)

The engine is usable three ways:

- **Embedded** — `from minidb import Database` and call `execute`.
- **DB-API 2.0** (`dbapi.py`) — a [PEP 249](https://peps.python.org/pep-0249/)
  driver: `connect`, `Connection`, `Cursor`, `fetchone/many/all`, `description`,
  `rowcount`, `executemany`, the standard exception hierarchy, and `with`
  support. It runs with autocommit off (per the spec): a transaction opens
  lazily on the first statement and closes on `commit()` / `rollback()`. This is
  what lets existing Python code and tooling talk to minidb unchanged.
- **Client/server** (`server.py` + `client.py`) — a threaded TCP server that
  wraps one `Database`. Every request runs under a single global lock, so
  concurrent clients are **serialized** (the SQLite model): consistent, simple,
  and correct, at the cost of write parallelism. Transactions are owned per
  connection — one connection holds the write path between `BEGIN` and
  `COMMIT`/`ROLLBACK`, others are told the database is locked, and a dropped
  connection rolls its transaction back. The wire protocol is newline-delimited
  JSON, so a client in any language is trivial to write.

**Honest limits.** Because a file database is one process's `Database`, the
embedded and DB-API paths assume a single process per file (like opening SQLite
exclusively); for genuine multi-process/multi-client sharing, run the server.
Writes are serialized everywhere. These are deliberate, documented trade-offs,
not accidents.

## 10. The buffer pool (`storage/buffer_pool.py`)

Everything above storage talks to a **`BufferPool`**, not the pager directly:

```
engine → BufferPool → Pager → disk
```

The pool is a bounded LRU cache of page frames (`cache_pages`, default 1024). It
serves `read_page` / `write_page` / `allocate_page` / `free_page`, tracks
hits/misses/evictions (`db.cache_stats()`), and supports `pin`/`unpin`.

**Durability — a no-steal policy.** The pool never writes a dirty page to disk
on its own. Dirty pages hold the active transaction's uncommitted changes and
stay resident until commit, when `Database._commit` logs their after-images to
the WAL, fsyncs the commit record, and *only then* calls `pool.flush()` (which
writes them via the pager and fsyncs). This is exactly the WAL rule — **the log
is durable before the page reaches the main file** — and it is why `ROLLBACK` is
just `pool.discard()` (drop the dirty frames; reload meta from disk).

**Eviction** therefore only ever targets **clean, unpinned** frames (dropping
them is safe — they equal what's on disk). If every resident frame is dirty or
pinned, the pool grows past `capacity` rather than lose data or deadlock, so a
single transaction's dirty working set must fit in memory. Making dirty pages
stealable would require UNDO logging, which this build does not have — a
deliberate, documented limitation (no concurrent writers yet either).
