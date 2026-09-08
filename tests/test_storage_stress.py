"""Stress: many INSERT/UPDATE/DELETE/COMMIT/ROLLBACK cycles against a tiny
buffer pool (so eviction happens constantly), checked against a reference model
and verified to persist across a reopen."""

import random

from minidb import Database
from minidb.storage.btree import BPlusTree


def _snapshot(db):
    return {r[0]: r[1] for r in db.execute("SELECT id, v FROM t").rows}


def _val(n):
    # chunky rows so the table spans many pages -> the cache can't hold it all
    return f"{n}-" + "x" * 200


def test_stress_crud_with_forced_eviction(tmp_path):
    path = str(tmp_path / "s.db")
    db = Database(path, cache_pages=4)  # tiny cache -> constant eviction
    db.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")

    rng = random.Random(20240607)
    committed = {}  # reference model of the committed state

    for round_no in range(300):
        db.execute("BEGIN")
        pending = dict(committed)  # what this txn will have done so far
        for _ in range(rng.randint(1, 6)):
            r = rng.random()
            key = rng.randint(1, 120)
            if r < 0.45:  # insert or replace
                val = _val(rng.randint(0, 10_000))
                if key in pending:
                    db.execute("UPDATE t SET v = ? WHERE id = ?", (val, key))
                else:
                    db.execute("INSERT INTO t VALUES (?, ?)", (key, val))
                pending[key] = val
            elif r < 0.75:  # update if present
                if key in pending:
                    val = _val(rng.randint(0, 10_000))
                    db.execute("UPDATE t SET v = ? WHERE id = ?", (val, key))
                    pending[key] = val
            else:  # delete if present
                if key in pending:
                    db.execute("DELETE FROM t WHERE id = ?", (key,))
                    del pending[key]

        if rng.random() < 0.5:
            db.execute("COMMIT")
            committed = pending  # durable now
        else:
            db.execute("ROLLBACK")  # pending changes vanish

        if round_no % 25 == 0:
            assert _snapshot(db) == committed

    # final consistency + the PK tree is still structurally valid
    assert _snapshot(db) == committed
    BPlusTree(db.pager, db.catalog.get("t").root_page_id).validate()
    assert db.cache_stats()["evictions"] > 0  # eviction really exercised
    db.close()

    # everything survives a reopen (persistence through the pool + WAL)
    db2 = Database(path, cache_pages=6)
    assert _snapshot(db2) == committed
    db2.close()
