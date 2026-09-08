"""The public entry point: open a database, run SQL, manage transactions.

A :class:`Database` owns the pager, the write-ahead log, the catalog, and the
transaction manager, and wires them into a single ``execute(sql)`` method.

Transaction model
------------------
* Statements run in **autocommit** by default: each one gets its own
  transaction that commits on success and rolls back on error.
* ``BEGIN`` opens an explicit transaction; subsequent statements buffer their
  writes until ``COMMIT`` (flush + WAL) or ``ROLLBACK`` (discard buffered pages).

Commit path (WAL-first, force-at-commit): log every modified page's after-image
to the WAL, write a fsync'd COMMIT record, then flush the pages to the main file
and checkpoint (truncate) the WAL. See :mod:`minidb.storage.wal`.
"""

from __future__ import annotations

import os
from typing import Optional

from .engine.catalog import Catalog
from .engine.executor import Executor, Result
from .sql import ast
from .sql.parser import bind_parameters, parse
from .storage.pager import Pager
from .storage.wal import WAL
from .txn.mvcc import Transaction, TransactionManager


class Database:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._memory = path == ":memory:"
        if self._memory:
            # An in-memory database uses a throwaway temp file so the on-disk
            # machinery (pager/WAL) is exercised identically to a real file.
            import tempfile

            self._tmpdir = tempfile.mkdtemp(prefix="minidb-mem-")
            path = os.path.join(self._tmpdir, "db")

        self.pager = Pager(path)
        self.wal = WAL(path + ".wal")
        # Crash recovery: replay committed transactions, then checkpoint.
        self.wal.recover(self.pager)
        self.wal.truncate()

        self.catalog = Catalog(self.pager)
        self.txn_manager = TransactionManager(self.pager)
        self.executor = Executor(self)
        self.current_txn: Optional[Transaction] = None

    # -- public API --------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> Result:
        """Run one SQL statement.

        ``params`` fills ``?`` placeholders positionally; values are bound after
        parsing, so user data never becomes part of the SQL text (no injection).
        """
        stmt = parse(sql)
        stmt = bind_parameters(stmt, list(params))

        if isinstance(stmt, ast.Begin):
            if self.current_txn is not None:
                raise RuntimeError("already inside a transaction")
            self.current_txn = self.txn_manager.begin(autocommit=False)
            return Result("txn", message="BEGIN")

        if isinstance(stmt, ast.Commit):
            if self.current_txn is None:
                raise RuntimeError("no transaction in progress")
            self._commit(self.current_txn)
            self.current_txn = None
            return Result("txn", message="COMMIT")

        if isinstance(stmt, ast.Rollback):
            if self.current_txn is None:
                raise RuntimeError("no transaction in progress")
            self._rollback(self.current_txn)
            self.current_txn = None
            return Result("txn", message="ROLLBACK")

        # DDL / DML / SELECT
        if self.current_txn is not None:
            return self.executor.execute(stmt, self.current_txn)

        # autocommit
        txn = self.txn_manager.begin(autocommit=True)
        try:
            result = self.executor.execute(stmt, txn)
        except Exception:
            self._rollback(txn)
            raise
        self._commit(txn)
        return result

    def executescript(self, script: str) -> list[Result]:
        """Run several ``;``-separated statements, returning each result."""
        results = []
        for chunk in _split_statements(script):
            results.append(self.execute(chunk))
        return results

    # -- transaction plumbing ---------------------------------------------
    def _commit(self, txn: Transaction) -> None:
        if not self.pager.has_writes():
            # Read-only transaction: nothing durable to write. The advanced
            # next_txid is persisted lazily on the next writing commit.
            self.txn_manager.commit(txn)
            return
        for page_id, data in self.pager.dirty_pages().items():
            self.wal.log_page(txn.xid, page_id, data)
        self.wal.log_commit(txn.xid)  # fsync'd
        self.pager.flush()  # push pages to the main file
        self.wal.truncate()  # checkpoint: main file is now authoritative
        self.txn_manager.commit(txn)

    def _rollback(self, txn: Transaction) -> None:
        self.wal.log_abort(txn.xid)
        self.pager.discard()
        # in-memory schema roots may have advanced during the txn; reload
        self.catalog = Catalog(self.pager)
        self.txn_manager.abort(txn)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        if self.current_txn is not None:
            self._rollback(self.current_txn)
            self.current_txn = None
        self.wal.close()
        self.pager.close()
        if self._memory:
            import shutil

            shutil.rmtree(self._tmpdir, ignore_errors=True)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _split_statements(script: str) -> list[str]:
    """Split a script on semicolons that are not inside string literals."""
    out, buf, in_str = [], [], False
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c == "'":
            in_str = not in_str
            buf.append(c)
        elif c == ";" and not in_str:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out
