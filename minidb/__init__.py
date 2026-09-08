"""minidb - a small but real relational database engine, from scratch.

Layers (bottom to top):

* :mod:`minidb.storage.pager`  - single-file, fixed-size page storage
* :mod:`minidb.storage.wal`    - write-ahead log + crash recovery
* :mod:`minidb.storage.btree`  - persistent B+Tree (the table index)
* :mod:`minidb.sql`            - tokenizer, AST, recursive-descent parser
* :mod:`minidb.engine`         - catalog, types, planner, executor
* :mod:`minidb.txn.mvcc`       - MVCC snapshot-isolation transactions
* :mod:`minidb.database`       - the ``Database`` facade you actually use

Example
-------
>>> from minidb import Database
>>> db = Database(":memory:")
>>> db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
>>> db.execute("INSERT INTO t VALUES (1, 'ada')")
>>> db.execute("SELECT * FROM t").rows
[[1, 'ada']]
"""

from .database import Database
from .engine.executor import Result

__all__ = ["Database", "Result"]
__version__ = "0.2.0"
