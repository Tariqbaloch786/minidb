"""Turns a parsed statement into results, driving storage + MVCC.

The executor is deliberately the only place that knows how all the layers fit
together: it asks the :mod:`planner` for an access path, walks the B+Tree,
filters rows through the MVCC :class:`~minidb.txn.mvcc.TransactionManager`, and
writes new row versions on ``INSERT`` / ``UPDATE`` / ``DELETE``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..sql import ast
from ..storage.btree import BPlusTree
from ..txn.mvcc import Version, Transaction, decode_chain, encode_chain
from . import planner, types
from .catalog import TableSchema


class ExecutionError(Exception):
    pass


@dataclass
class Result:
    kind: str  # "select" | "dml" | "ddl" | "explain" | "txn"
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    rowcount: int = 0
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        if self.kind in ("select", "explain"):
            return f"{len(self.rows)} row(s)"
        return self.message


# -- 3-valued predicate evaluation -----------------------------------------
def _eval(expr: Any, row: dict[str, Any]) -> Optional[bool] | Any:
    if isinstance(expr, ast.Literal):
        return expr.value
    if isinstance(expr, ast.Column):
        return row.get(expr.name)
    if isinstance(expr, ast.BinOp):
        op = expr.op
        if op == "AND":
            lt, rt = _truth(_eval(expr.left, row)), _truth(_eval(expr.right, row))
            if lt is False or rt is False:
                return False
            if lt is None or rt is None:
                return None
            return True
        if op == "OR":
            lt, rt = _truth(_eval(expr.left, row)), _truth(_eval(expr.right, row))
            if lt is True or rt is True:
                return True
            if lt is None or rt is None:
                return None
            return False
        if op == "NOT":
            v = _truth(_eval(expr.left, row))
            return None if v is None else (not v)
        # comparison
        left = _eval(expr.left, row)
        right = _eval(expr.right, row)
        if left is None or right is None:
            return None
        try:
            if op == "=":
                return left == right
            if op == "!=":
                return left != right
            if op == "<":
                return left < right
            if op == "<=":
                return left <= right
            if op == ">":
                return left > right
            if op == ">=":
                return left >= right
        except TypeError:
            return None
    raise ExecutionError(f"cannot evaluate expression {expr!r}")


def _truth(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _passes(where: Optional[Any], row: dict[str, Any]) -> bool:
    if where is None:
        return True
    return _truth(_eval(where, row)) is True


class Executor:
    def __init__(self, db):
        self.db = db  # Database instance (catalog, pager, txn_manager)

    # -- helpers -----------------------------------------------------------
    def _tree(self, schema: TableSchema) -> BPlusTree:
        return BPlusTree(self.db.pager, schema.root_page_id)

    def _save_root_if_changed(self, schema: TableSchema, tree: BPlusTree) -> None:
        if tree.root != schema.root_page_id:
            schema.root_page_id = tree.root
            self.db.catalog.save()

    def _require_table(self, name: str) -> TableSchema:
        schema = self.db.catalog.get(name)
        if schema is None:
            raise ExecutionError(f"no such table: {name}")
        return schema

    # -- dispatch ----------------------------------------------------------
    def execute(self, stmt: Any, txn: Transaction) -> Result:
        if isinstance(stmt, ast.CreateTable):
            return self._create_table(stmt)
        if isinstance(stmt, ast.DropTable):
            return self._drop_table(stmt)
        if isinstance(stmt, ast.Insert):
            return self._insert(stmt, txn)
        if isinstance(stmt, ast.Select):
            return self._select(stmt, txn)
        if isinstance(stmt, ast.Update):
            return self._update(stmt, txn)
        if isinstance(stmt, ast.Delete):
            return self._delete(stmt, txn)
        raise ExecutionError(f"cannot execute {stmt!r}")

    # -- DDL ---------------------------------------------------------------
    def _create_table(self, stmt: ast.CreateTable) -> Result:
        if self.db.catalog.get(stmt.name) is not None:
            raise ExecutionError(f"table {stmt.name!r} already exists")
        pk_cols = [c for c in stmt.columns if c.primary_key]
        pk = pk_cols[0]
        if pk.type != "INT":
            raise ExecutionError("PRIMARY KEY column must be of type INT")
        tree = BPlusTree.create(self.db.pager)
        schema = TableSchema(
            name=stmt.name,
            columns=[(c.name, c.type) for c in stmt.columns],
            pk=pk.name,
            not_null=[c.name for c in stmt.columns if c.not_null],
            root_page_id=tree.root,
        )
        self.db.catalog.add(schema)
        self.db.catalog.save()
        return Result("ddl", message=f"CREATE TABLE {stmt.name}")

    def _drop_table(self, stmt: ast.DropTable) -> Result:
        self._require_table(stmt.name)
        self.db.catalog.drop(stmt.name)
        self.db.catalog.save()
        return Result("ddl", message=f"DROP TABLE {stmt.name}")

    # -- INSERT ------------------------------------------------------------
    def _insert(self, stmt: ast.Insert, txn: Transaction) -> Result:
        schema = self._require_table(stmt.table)
        cols = stmt.columns or schema.column_names
        for c in cols:
            if not schema.has_column(c):
                raise ExecutionError(f"no such column: {c}")
        tree = self._tree(schema)
        count = 0
        for values in stmt.rows:
            if len(values) != len(cols):
                raise ExecutionError("INSERT column/value count mismatch")
            row: dict[str, Any] = {name: None for name in schema.column_names}
            for name, lit in zip(cols, values):
                row[name] = types.coerce(lit.value, schema.type_of(name), name)
            self._check_constraints(schema, row)
            pk_val = row[schema.pk]
            existing = tree.get(pk_val)
            chain = decode_chain(bytes(existing)) if existing is not None else []
            if self.db.txn_manager.visible_row(chain, txn) is not None:
                raise ExecutionError(f"duplicate primary key: {pk_val}")
            chain = self.db.txn_manager.compact(chain)
            chain.append(Version(txn.xid, 0, False, types.serialize_row(schema.columns, row)))
            tree.insert(pk_val, encode_chain(chain))
            count += 1
        self._save_root_if_changed(schema, tree)
        return Result("dml", rowcount=count, message=f"INSERT {count}")

    def _check_constraints(self, schema: TableSchema, row: dict[str, Any]) -> None:
        for name in schema.not_null:
            if row.get(name) is None:
                raise ExecutionError(f"NULL value in NOT NULL column {name!r}")
        if row.get(schema.pk) is None:
            raise ExecutionError(f"primary key {schema.pk!r} cannot be NULL")

    # -- scanning ----------------------------------------------------------
    def _scan(self, schema: TableSchema, plan: planner.Plan, txn: Transaction):
        """Yield (pk, row_dict, version) for rows visible to ``txn``."""
        tree = self._tree(schema)
        if plan.method == planner.INDEX_SEEK:
            raw = tree.get(plan.seek_key)
            candidates = [(plan.seek_key, raw)] if raw is not None else []
        elif plan.method == planner.INDEX_RANGE:
            candidates = list(tree.range(plan.lo, plan.hi))
        else:
            candidates = list(tree.items())
        for pk, raw in candidates:
            chain = decode_chain(bytes(raw))
            version = self.db.txn_manager.visible_version(chain, txn)
            if version is None:
                continue
            row = types.deserialize_row(schema.columns, version.data)
            if _passes(plan.residual, row):
                yield pk, row, version

    # -- SELECT ------------------------------------------------------------
    def _select(self, stmt: ast.Select, txn: Transaction) -> Result:
        schema = self._require_table(stmt.table)
        plan = planner.plan_scan(stmt.table, schema.pk, stmt.where)

        if stmt.explain:
            return Result("explain", columns=["QUERY PLAN"], rows=[[plan.describe()]])

        if stmt.columns == ["*"]:
            out_cols = schema.column_names
        else:
            for c in stmt.columns:
                if not schema.has_column(c):
                    raise ExecutionError(f"no such column: {c}")
            out_cols = stmt.columns

        rows = [row for _, row, _ in self._scan(schema, plan, txn)]

        if stmt.order_by is not None:
            col = stmt.order_by.column
            if not schema.has_column(col):
                raise ExecutionError(f"no such column: {col}")
            rows.sort(key=lambda r: (r.get(col) is None, r.get(col)),
                      reverse=stmt.order_by.descending)

        if stmt.limit is not None:
            rows = rows[: stmt.limit]

        projected = [[r.get(c) for c in out_cols] for r in rows]
        return Result("select", columns=out_cols, rows=projected)

    # -- UPDATE ------------------------------------------------------------
    def _update(self, stmt: ast.Update, txn: Transaction) -> Result:
        schema = self._require_table(stmt.table)
        for col, _ in stmt.assignments:
            if not schema.has_column(col):
                raise ExecutionError(f"no such column: {col}")
            if col == schema.pk:
                raise ExecutionError("cannot UPDATE the primary key column")
        plan = planner.plan_scan(stmt.table, schema.pk, stmt.where)
        tree = self._tree(schema)
        count = 0
        # materialize first so we don't mutate the tree while scanning it
        targets = list(self._scan(schema, plan, txn))
        for pk, row, _ in targets:
            new_row = dict(row)
            for col, lit in stmt.assignments:
                new_row[col] = types.coerce(lit.value, schema.type_of(col), col)
            self._check_constraints(schema, new_row)
            chain = decode_chain(bytes(tree.get(pk)))
            version = self.db.txn_manager.visible_version(chain, txn)
            if version is None:
                continue  # changed under us; skip
            version.xmax = txn.xid
            chain.append(Version(txn.xid, 0, False, types.serialize_row(schema.columns, new_row)))
            chain = self.db.txn_manager.compact(chain)
            tree.insert(pk, encode_chain(chain))
            count += 1
        self._save_root_if_changed(schema, tree)
        return Result("dml", rowcount=count, message=f"UPDATE {count}")

    # -- DELETE ------------------------------------------------------------
    def _delete(self, stmt: ast.Delete, txn: Transaction) -> Result:
        schema = self._require_table(stmt.table)
        plan = planner.plan_scan(stmt.table, schema.pk, stmt.where)
        tree = self._tree(schema)
        count = 0
        targets = list(self._scan(schema, plan, txn))
        for pk, _, _ in targets:
            chain = decode_chain(bytes(tree.get(pk)))
            version = self.db.txn_manager.visible_version(chain, txn)
            if version is None:
                continue
            version.xmax = txn.xid
            chain.append(Version(txn.xid, 0, True, b""))  # tombstone
            chain = self.db.txn_manager.compact(chain)
            if chain:
                tree.insert(pk, encode_chain(chain))
            else:
                tree.delete(pk)
            count += 1
        self._save_root_if_changed(schema, tree)
        return Result("dml", rowcount=count, message=f"DELETE {count}")
