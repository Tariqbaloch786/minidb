# MiniDB — Engineering Audit & Evolution Plan

> Companion to [`ARCHITECTURE.md`](ARCHITECTURE.md) (which explains how the
> engine works today). This document is the **audit**: current architecture,
> concrete weaknesses, the target architecture, and the ordered migration plan
> used to evolve MiniDB into a robust single-node relational engine.
>
> Named `AUDIT.md` rather than `architecture.md` because the repository already
> ships `ARCHITECTURE.md`, and the project's primary filesystem (Windows) is
> case-insensitive — the two names would collide.

## 1. Current architecture (as audited)

```
SQL text
  → tokenizer  (sql/tokenizer.py)
  → parser → AST  (sql/parser.py, sql/ast.py)      params bound on the AST
  → executor  (engine/executor.py)                 procedural, not operator-based
      ├─ planner  (engine/planner.py)              rule-only: PK seek/range vs seq scan
      ├─ catalog  (engine/catalog.py)              schemas persisted in a page chain
      └─ MVCC  (txn/mvcc.py)                        version chains, snapshot visibility
  → B+Tree  (storage/btree.py)                      one tree per table, PK is the key
  → Pager  (storage/pager.py)                       fixed 4 KiB pages, unbounded cache
  → WAL  (storage/wal.py)                           redo log, force-at-commit
  → single file on disk
Facade: database.py (execute + txn lifecycle) · dbapi.py (PEP 249) · server.py/client.py (TCP)
```

**What is genuinely good and must be preserved:**

- The **WAL redo protocol** is correct: after-images + `COMMIT` record fsynced
  *before* the main-file flush; recovery replays only committed txids; per-record
  CRC32 truncates a torn tail. This is a real durability design, not a toy.
- **MVCC visibility** (`xmin`/`xmax`, snapshot `active` set, `snapshot_xmax`) is
  sound for snapshot isolation, and the "unknown xid ⇒ committed" inference is
  justified by the force-at-commit rule (aborted txns never flush pages).
- **Clean layering** already exists: storage knows nothing about SQL; the parser
  knows nothing about pages. This is the right skeleton to build on.
- **Parameterized queries** bind on the AST (no string splicing) — injection-safe.
- Predicate → **index seek/range** pushdown for the primary key.

## 2. Critical weaknesses (grounded in the code)

### Storage / durability
- **No page checksums.** `pager._read_raw` silently zero-pads short reads and
  never verifies integrity. A torn write or bit-rot on any non-meta page is read
  as valid → wrong results or a `struct` crash. *(Highest-value storage gap.)*
- **The meta page (page 0) is unprotected.** A torn meta write can only be
  repaired via WAL replay, and only while the WAL still holds that page image.
- **No real buffer pool.** `pager._cache` is unbounded and is *cleared on every
  flush*, so there is no cross-transaction caching, no eviction policy, and no
  hit/miss accounting. Large databases will thrash.

### B+Tree
- **Delete never rebalances** (`btree.py:214`): it removes the key from a leaf
  and stops. Emptied leaves persist, linked and non-freed; the free list is
  never fed by tree shrinkage. No merge, no redistribution, no root collapse.
- **No `validate()`** — there is no way to assert the structural invariants a
  B+Tree must hold, and no randomized/property tests exercising delete.

### Indexing
- **No secondary indexes.** The PK B+Tree is the only index. The catalog has no
  index metadata; the planner can only ever choose the PK. Any non-PK predicate
  is a full scan. *(The mission's flagged "major priority".)*

### Values
- **No overflow pages.** A single value must fit one page (`_split_leaf` raises
  "value too large"). Large TEXT/BLOB is impossible.

### Concurrency
- **Single writer.** The server serializes everything under one global lock; the
  engine has one `current_txn`. No lock manager, no deadlock detection; readers
  and writers cannot truly overlap.

### Query engine & optimizer
- **Executor is procedural, not operator-based.** `_select` is one large method;
  joins and aggregation **fully materialize** in Python lists — no streaming, no
  operator tree, so large intermediates blow up memory.
- **Planner is rule-only.** No statistics, no cost model, no join-order or
  join-strategy choice (only index-nested-loop on a PK equi-join).

### SQL surface
- Missing: arithmetic expressions, `CASE`/`COALESCE`/`CAST`, `IN`/`BETWEEN`/
  `LIKE`, `DISTINCT`, `OFFSET`, `RIGHT`/`FULL JOIN`, `UNION`, CTEs, subqueries.

### Constraints
- Only `PRIMARY KEY` + `NOT NULL`. No standalone `UNIQUE`, `CHECK`, or
  `FOREIGN KEY`.

### Product surface
- **Server has no authentication/authorization** — it exposes the database to
  anyone who can reach the port.
- **No structured error hierarchy** (`ExecutionError`/`ParseError` are flat).
- **No observability** (`PRAGMA`s, metrics, structured logs).

## 3. Target architecture

```
        SQL ──▶ Parser/AST ──▶ Binder+Analyzer ──▶ Cost-based Optimizer
                                                          │
                                                          ▼
                                     Operator tree (SeqScan, IndexScan, Filter,
                                     Project, Sort, Limit, Hash/Merge/NLJoin, Agg)
                                                          │
                   ┌──────────────────────────────────────┼───────────────────┐
                   ▼                                        ▼                   ▼
             Transaction Mgr                             Catalog            Statistics
             (MVCC + lock mgr)                         (tables+indexes)     (histograms)
                   │                                        │
                   └───────────────────┬────────────────────┘
                                       ▼
                                  Access methods
                              (B+Tree: PK + secondary,
                               overflow chains)
                                       │
                                       ▼
                                  Buffer Pool  (pin/unpin, LRU/CLOCK, dirty set, stats)
                                       │
                                       ▼
                                  Pager (checksummed pages, free list)
                                       │
                              ┌────────┴────────┐
                              ▼                 ▼
                            Disk               WAL (LSN, checkpoints, recovery)
```

Boundaries to enforce: **storage never imports SQL; the optimizer never touches
files; access methods go through the buffer pool, not raw disk.**

## 4. Migration strategy

Evolve in **small, isolated, always-green phases**. Every phase: implement →
add tests (incl. property/fuzz where structural) → run the full suite → update
docs → leave the tree functional and committable. No big-bang rewrite. On-disk
format changes bump the file magic (`MDB1` → `MDB2`) and are documented.

## 5. Ordered roadmap

**Tier 1 — Foundation (correctness of storage & access)**
1. ✅ Audit + this document.
2. ✅ **B+Tree delete: merge / redistribute / root-collapse + `validate()` + fuzz.**
3. Page checksums + corruption detection (`CorruptionError`); protect meta. *(format bump)* ← *next*
4. Real buffer pool: bounded cache, pin/unpin, CLOCK/LRU eviction, hit/miss stats.
5. ✅ **Secondary indexes:** `CREATE/DROP INDEX`, catalog metadata, order-preserving key encoding, maintenance on DML, planner selection, equality + range scans, unique/non-unique/composite.
6. Overflow pages for large values.
7. WAL/recovery hardening: LSNs, checkpoint records, expanded crash-injection tests.

**Tier 2 — Database correctness**
8. MVCC hardening + documented isolation levels. 9. Lock manager + deadlock
detection. 10. Concurrent transactions. 11. `VACUUM`. 12. `UNIQUE`/`CHECK`/`FOREIGN KEY`.

**Tier 3 — Query engine**
13. Operator-based, streaming executor. 14–15. Hash/Merge joins. 16–18. Cost-based
optimizer + statistics. 19. `EXPLAIN ANALYZE`.

**Tier 4 — SQL** 20. Expression/predicate expansion. 21. CTEs. 22. Set ops. 23. Savepoints.

**Tier 5 — Product quality** 24. CLI. 25. Server auth. 26. Observability.
27. Docs set. 28. Benchmarks vs SQLite (comparison only). 29. Fuzzing.

## 6. Honest status

MiniDB is a **SQLite-class embedded engine** with a correct WAL/MVCC core and a
clean layering, but with an incomplete access layer (no secondary indexes, no
delete rebalancing, no page checksums) and a procedural query engine. It is
**not** production-ready and will not be claimed so until it has demonstrated
crash recovery under fault injection, index consistency, concurrency
correctness, corruption detection, and fuzz/perf testing. This document is the
plan to get there, phase by phase.
