"""Micro-benchmarks for minidb.

Not a competition with SQLite - just enough to show the engine behaves the way
the design claims (index seeks beat sequential scans, writes are durable) and to
give honest throughput numbers.

Run::

    python benchmarks/bench.py
    python benchmarks/bench.py --rows 100000
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from minidb import Database


def _timed(label, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"{label:<42} {elapsed * 1000:9.1f} ms")
    return result, elapsed


def run(rows: int, on_disk: bool) -> None:
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "bench.db") if on_disk else ":memory:"
    where = path if on_disk else "in-memory"
    print(f"minidb benchmark  -  {rows:,} rows  ({where})")
    print("-" * 60)

    db = Database(path)
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT, score INT)")

    keys = list(range(rows))
    random.shuffle(keys)

    def bulk_insert():
        # one explicit transaction => one fsync instead of `rows` fsyncs
        db.execute("BEGIN")
        for k in keys:
            db.execute(f"INSERT INTO t VALUES ({k}, 'user{k}', {k % 1000})")
        db.execute("COMMIT")

    _, t_insert = _timed(f"insert {rows:,} rows (1 txn)", bulk_insert)
    print(f"{'  -> inserts/sec':<42} {rows / t_insert:9.0f}")

    sample = random.sample(range(rows), min(1000, rows))

    def point_lookups():
        for k in sample:
            db.execute(f"SELECT * FROM t WHERE id = {k}")

    _, t_seek = _timed(f"{len(sample)} index seeks", point_lookups)
    print(f"{'  -> seeks/sec':<42} {len(sample) / t_seek:9.0f}")

    def seq_scan():
        db.execute("SELECT * FROM t WHERE score = 42")

    _timed("full sequential scan + filter", seq_scan)

    def range_scan():
        db.execute(f"SELECT * FROM t WHERE id >= {rows // 2} AND id < {rows // 2 + 100}")

    _timed("index range scan (100 rows)", range_scan)

    db.close()
    print("-" * 60)
    print("done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="minidb benchmark")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--disk", action="store_true", help="use a real file, not memory")
    args = parser.parse_args()
    run(args.rows, args.disk)


if __name__ == "__main__":
    main()
