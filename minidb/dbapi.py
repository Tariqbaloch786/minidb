"""A PEP 249 (DB-API 2.0) driver for minidb.

This is the interface that makes minidb *usable by real applications*: it's the
same ``connect().cursor().execute().fetchall()`` shape as ``sqlite3``,
``psycopg2`` and every other Python database driver, so existing code and tools
speak to it without changes.

Example
-------
>>> import minidb.dbapi as db
>>> conn = db.connect("app.db")
>>> cur = conn.cursor()
>>> cur.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
>>> cur.executemany("INSERT INTO t VALUES (?, ?)", [(1, "ada"), (2, "grace")])
>>> cur.execute("SELECT name FROM t WHERE id = ?", (1,))
>>> cur.fetchone()
('ada',)
>>> conn.commit()

Transactions are **not** autocommit (per the spec): work is grouped into a
transaction that starts on the first statement and ends on ``commit()`` /
``rollback()``. Parameters use the ``qmark`` style (``?``) and are bound safely,
so user input never becomes part of the SQL text.
"""

from __future__ import annotations

from typing import Optional

from .database import Database

# -- module globals required by PEP 249 ------------------------------------
apilevel = "2.0"
threadsafety = 1  # threads may share the module, but not connections
paramstyle = "qmark"


# -- exception hierarchy (PEP 249) -----------------------------------------
class Warning(Exception):  # noqa: A001 - name mandated by PEP 249
    pass


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


def _translate(exc: Exception) -> Error:
    """Map an internal minidb error onto the DB-API exception hierarchy."""
    msg = str(exc)
    low = msg.lower()
    if any(s in low for s in ("duplicate primary key", "not null", "cannot be null")):
        return IntegrityError(msg)
    if exc.__class__.__name__ == "ParseError":
        return ProgrammingError(msg)
    if exc.__class__.__name__ == "ExecutionError":
        return ProgrammingError(msg)
    return DatabaseError(msg)


# -- Connection / Cursor ---------------------------------------------------
class Connection:
    def __init__(self, database: str = ":memory:"):
        self._db = Database(database)
        self._in_txn = False
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("connection is closed")

    def _run(self, sql: str, params: tuple):
        self._check_open()
        head = sql.lstrip()[:9].upper()
        is_txn_ctl = head.startswith(("BEGIN", "COMMIT", "ROLLBACK"))
        if not is_txn_ctl and not self._in_txn:
            self._db.execute("BEGIN")
            self._in_txn = True
        try:
            result = self._db.execute(sql, params)
        except Error:
            raise
        except Exception as exc:  # noqa: BLE001 - translate to DB-API errors
            raise _translate(exc) from exc
        if is_txn_ctl:
            self._in_txn = head.startswith("BEGIN")
        return result

    def cursor(self) -> "Cursor":
        self._check_open()
        return Cursor(self)

    def execute(self, sql: str, params: tuple = ()) -> "Cursor":
        """Convenience: create a cursor, run one statement, return the cursor."""
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self) -> None:
        self._check_open()
        if self._in_txn:
            self._db.execute("COMMIT")
            self._in_txn = False

    def rollback(self) -> None:
        self._check_open()
        if self._in_txn:
            self._db.execute("ROLLBACK")
            self._in_txn = False

    def close(self) -> None:
        if self._closed:
            return
        if self._in_txn:
            try:
                self._db.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
        self._db.close()
        self._closed = True

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()


class Cursor:
    def __init__(self, conn: Connection):
        self._conn = conn
        self.arraysize = 1
        self.rowcount = -1
        self.description: Optional[list[tuple]] = None
        self._rows: list[tuple] = []
        self._pos = 0
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise ProgrammingError("cursor is closed")
        self._conn._check_open()

    def execute(self, sql: str, params: tuple = ()) -> "Cursor":
        self._check_open()
        result = self._conn._run(sql, tuple(params))
        if result.kind in ("select", "explain"):
            self.description = [
                (name, None, None, None, None, None, None) for name in result.columns
            ]
            self._rows = [tuple(row) for row in result.rows]
            self.rowcount = len(self._rows)
        else:
            self.description = None
            self._rows = []
            self.rowcount = result.rowcount
        self._pos = 0
        return self

    def executemany(self, sql: str, seq_of_params) -> "Cursor":
        total = 0
        for params in seq_of_params:
            self.execute(sql, params)
            if self.rowcount and self.rowcount > 0:
                total += self.rowcount
        self.rowcount = total
        self.description = None
        self._rows = []
        return self

    def fetchone(self) -> Optional[tuple]:
        self._check_open()
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size: Optional[int] = None) -> list[tuple]:
        self._check_open()
        size = self.arraysize if size is None else size
        chunk = self._rows[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def fetchall(self) -> list[tuple]:
        self._check_open()
        chunk = self._rows[self._pos :]
        self._pos = len(self._rows)
        return chunk

    def __iter__(self):
        return self

    def __next__(self) -> tuple:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self) -> None:
        self._closed = True

    # PEP 249 optional no-ops
    def setinputsizes(self, sizes) -> None:  # pragma: no cover
        pass

    def setoutputsize(self, size, column=None) -> None:  # pragma: no cover
        pass


def connect(database: str = ":memory:") -> Connection:
    """Open a connection to a minidb database file (or ``":memory:"``)."""
    return Connection(database)
