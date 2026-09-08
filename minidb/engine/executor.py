"""Turns a parsed statement into results, driving storage + MVCC.

The executor is deliberately the only place that knows how all the layers fit
together: it asks the :mod:`planner` for an access path, walks the B+Tree,
filters rows through the MVCC :class:`~minidb.txn.mvcc.TransactionManager`, and
writes new row versions on ``INSERT`` / ``UPDATE`` / ``DELETE``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from ..sql import ast
from ..storage.btree import BPlusTree
from ..txn.mvcc import Transaction, Version, decode_chain, encode_chain
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
    if isinstance(expr, ast.Aggregate):
        return row.get(expr.label)
    if isinstance(expr, ast.Column):
        key = expr.name if expr.table is None else f"{expr.table}.{expr.name}"
        return row.get(key)
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


# -- join / namespace helpers ----------------------------------------------
def _multi_ns(table: str, schema: TableSchema, row: dict[str, Any],
              ambiguous: frozenset) -> dict[str, Any]:
    """Build an evaluation namespace: qualified ``table.col`` keys always, and
    bare ``col`` keys only for names that are unambiguous across the join."""
    ns: dict[str, Any] = {}
    for cname, _ in schema.columns:
        val = row.get(cname)
        ns[f"{table}.{cname}"] = val
        if cname not in ambiguous:
            ns[cname] = val
    return ns


def _columns_in(expr: Any):
    """Column references that are NOT inside an aggregate call."""
    if isinstance(expr, ast.Column):
        yield expr
    elif isinstance(expr, ast.BinOp):
        yield from _columns_in(expr.left)
        yield from _columns_in(expr.right)


def _aggregates_in(expr: Any):
    if isinstance(expr, ast.Aggregate):
        yield expr
    elif isinstance(expr, ast.BinOp):
        yield from _aggregates_in(expr.left)
        yield from _aggregates_in(expr.right)


def _contains_aggregate(expr: Any) -> bool:
    return next(_aggregates_in(expr), None) is not None


def _flatten_and(expr: Any) -> list[Any]:
    if isinstance(expr, ast.BinOp) and expr.op == "AND":
        return _flatten_and(expr.left) + _flatten_and(expr.right)
    return [expr]


def _and_of(conjuncts: list[Any]) -> Optional[Any]:
    if not conjuncts:
        return None
    node = conjuncts[0]
    for c in conjuncts[1:]:
        node = ast.BinOp("AND", node, c)
    return node


def _conjuncts_for_table(where: Optional[Any], table: str, table_of) -> Optional[Any]:
    """The AND-conjuncts of ``where`` that reference only ``table`` (for pushdown)."""
    if where is None:
        return None
    kept = []
    for c in _flatten_and(where):
        cols = list(_columns_in(c))
        if cols and all(table_of(col) == table for col in cols):
            kept.append(c)
    return _and_of(kept)


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
            if _passes(plan.residual, _multi_ns(schema.name, schema, row, frozenset())):
                yield pk, row, version

    # -- SELECT (single table or joins) -----------------------------------
    def _select(self, stmt: ast.Select, txn: Transaction) -> Result:
        base_schema = self._require_table(stmt.table)
        table_list = [(stmt.table, base_schema)]
        for j in stmt.joins:
            table_list.append((j.table, self._require_table(j.table)))
        if len({t for t, _ in table_list}) != len(table_list):
            raise ExecutionError("a table may not appear twice in FROM/JOIN "
                                 "(aliases are not supported yet)")
        schemas = {t: s for t, s in table_list}
        counts = Counter(c for _, s in table_list for c, _ in s.columns)
        ambiguous = frozenset(n for n, c in counts.items() if c > 1)

        def table_of(col: ast.Column) -> str:
            if col.table is not None:
                if col.table not in schemas:
                    raise ExecutionError(f"unknown table qualifier: {col.table}")
                if col.name != "*" and not schemas[col.table].has_column(col.name):
                    raise ExecutionError(f"no such column: {col.table}.{col.name}")
                return col.table
            if col.name in ambiguous:
                raise ExecutionError(
                    f"ambiguous column: {col.name} (qualify it with a table name)")
            for t, s in table_list:
                if s.has_column(col.name):
                    return t
            raise ExecutionError(f"no such column: {col.name}")

        def key_of(col: ast.Column) -> str:
            t = table_of(col)
            return col.name if col.table is None else f"{t}.{col.name}"

        # Aggregates belong in SELECT/HAVING, never in WHERE or a join ON.
        if _contains_aggregate(stmt.where):
            raise ExecutionError("aggregate functions are not allowed in WHERE (use HAVING)")
        for j in stmt.joins:
            if _contains_aggregate(j.on):
                raise ExecutionError("aggregate functions are not allowed in a JOIN ON clause")

        # Validate every column reference up front (clear errors + safe eval).
        for col in _columns_in(stmt.where):
            table_of(col)
        for j in stmt.joins:
            for col in _columns_in(j.on):
                table_of(col)
        if stmt.order_by is not None and isinstance(stmt.order_by.column, ast.Column):
            table_of(stmt.order_by.column)
        for item in stmt.columns:
            if isinstance(item, ast.Aggregate):
                if item.column.name != "*":
                    table_of(item.column)
            elif item.name == "*":
                if item.table is not None and item.table not in schemas:
                    raise ExecutionError(f"unknown table qualifier: {item.table}")
            else:
                table_of(item)

        # Push WHERE conjuncts that only touch the base table into its scan.
        base_where = _conjuncts_for_table(stmt.where, stmt.table, table_of)
        base_plan = planner.plan_scan(stmt.table, base_schema.pk, base_where)

        if stmt.explain:
            return Result("explain", columns=["QUERY PLAN"],
                          rows=self._explain_rows(stmt, table_list, base_plan, key_of))

        rows = [
            _multi_ns(stmt.table, base_schema, row, ambiguous)
            for _, row, _ in self._scan(base_schema, base_plan, txn)
        ]
        for j in stmt.joins:
            rows = self._apply_join(rows, j, schemas[j.table], txn, ambiguous, key_of)

        # Residual WHERE (join-spanning / non-base predicates) over joined rows.
        rows = [ns for ns in rows if _passes(stmt.where, ns)]

        grouped = bool(stmt.group_by) or stmt.having is not None or any(
            isinstance(it, ast.Aggregate) for it in stmt.columns)
        if grouped:
            return self._aggregate(stmt, rows, table_of, key_of, ambiguous)

        if stmt.order_by is not None:
            okey = key_of(stmt.order_by.column)
            rows.sort(key=lambda ns: (ns.get(okey) is None, ns.get(okey)),
                      reverse=stmt.order_by.descending)
        if stmt.limit is not None:
            rows = rows[: stmt.limit]

        out = self._projection(stmt.columns, table_list, ambiguous)
        columns = [label for label, _ in out]
        projected = [[ns.get(key) for _, key in out] for ns in rows]
        return Result("select", columns=columns, rows=projected)

    # -- aggregation / GROUP BY -------------------------------------------
    def _aggregate(self, stmt, rows, table_of, key_of, ambiguous):
        group_cols = stmt.group_by
        group_keys = [key_of(c) for c in group_cols]
        group_key_set = set(group_keys)

        # Non-aggregate SELECT columns must be part of GROUP BY.
        for item in stmt.columns:
            if isinstance(item, ast.Aggregate):
                continue
            if item.name == "*":
                raise ExecutionError("SELECT * cannot be combined with aggregation")
            if key_of(item) not in group_key_set:
                raise ExecutionError(
                    f"column {item.name!r} must appear in GROUP BY or inside an aggregate")

        # Columns used bare in HAVING must also be grouped.
        for col in _columns_in(stmt.having):
            if key_of(col) not in group_key_set:
                raise ExecutionError(
                    f"column {col.name!r} in HAVING must appear in GROUP BY or an aggregate")

        # Every aggregate we need to materialize (SELECT + HAVING + ORDER BY).
        needed: dict[str, ast.Aggregate] = {}
        for item in stmt.columns:
            if isinstance(item, ast.Aggregate):
                needed[item.label] = item
        for a in _aggregates_in(stmt.having):
            needed[a.label] = a
        if stmt.order_by is not None and isinstance(stmt.order_by.column, ast.Aggregate):
            needed[stmt.order_by.column.label] = stmt.order_by.column
        for a in needed.values():
            if a.column.name != "*":
                table_of(a.column)  # validate the aggregate's argument

        def compute(agg: ast.Aggregate, group: list[dict]) -> Any:
            if agg.func == "COUNT" and agg.column.name == "*":
                return len(group)
            akey = key_of(agg.column)
            values = [r.get(akey) for r in group]
            nonnull = [v for v in values if v is not None]
            if agg.func == "COUNT":
                return len(nonnull)
            if not nonnull:
                return None  # SUM/AVG/MIN/MAX over no rows is NULL
            if agg.func == "SUM":
                return sum(nonnull)
            if agg.func == "AVG":
                return sum(nonnull) / len(nonnull)
            if agg.func == "MIN":
                return min(nonnull)
            if agg.func == "MAX":
                return max(nonnull)
            raise ExecutionError(f"unknown aggregate: {agg.func}")

        # Partition rows into groups (first-seen order preserved).
        groups: dict[tuple, list[dict]] = {}
        order: list[tuple] = []
        for ns in rows:
            gk = tuple(ns.get(k) for k in group_keys)
            if gk not in groups:
                groups[gk] = []
                order.append(gk)
            groups[gk].append(ns)
        # Aggregate with no GROUP BY over an empty table still yields one row.
        if not group_keys and not order:
            groups[()] = []
            order = [()]

        out_rows = []
        for gk in order:
            group = groups[gk]
            gns: dict[str, Any] = {}
            for col, k in zip(group_cols, group_keys):
                val = group[0].get(k) if group else None
                gns[k] = val
                gns[f"{table_of(col)}.{col.name}"] = val
                if col.name not in ambiguous:
                    gns[col.name] = val
            for label, agg in needed.items():
                gns[label] = compute(agg, group)
            if stmt.having is not None and not _passes(stmt.having, gns):
                continue
            out_rows.append(gns)

        if stmt.order_by is not None:
            oc = stmt.order_by.column
            okey = oc.label if isinstance(oc, ast.Aggregate) else key_of(oc)
            out_rows.sort(key=lambda ns: (ns.get(okey) is None, ns.get(okey)),
                          reverse=stmt.order_by.descending)
        if stmt.limit is not None:
            out_rows = out_rows[: stmt.limit]

        labels, keys = [], []
        for item in stmt.columns:
            if isinstance(item, ast.Aggregate):
                labels.append(item.label)
                keys.append(item.label)
            else:
                labels.append(item.name)
                keys.append(key_of(item))
        projected = [[gns.get(k) for k in keys] for gns in out_rows]
        return Result("select", columns=labels, rows=projected)

    # -- join execution ---------------------------------------------------
    def _apply_join(self, rows, join, inner_schema, txn, ambiguous, key_of):
        """Nested-loop join; uses an index seek on the inner table when the ON
        clause is an equi-join against the inner primary key."""
        drive = self._equijoin_drive(join, inner_schema, key_of)
        out = []
        for outer in rows:
            matched = False
            for inner_row in self._inner_candidates(join, inner_schema, outer, txn, drive):
                combined = dict(outer)
                for cname, _ in inner_schema.columns:
                    val = inner_row.get(cname)
                    combined[f"{join.table}.{cname}"] = val
                    if cname not in ambiguous:
                        combined[cname] = val
                if _passes(join.on, combined):
                    out.append(combined)
                    matched = True
            if join.kind == "LEFT" and not matched:
                combined = dict(outer)
                for cname, _ in inner_schema.columns:
                    combined[f"{join.table}.{cname}"] = None
                    if cname not in ambiguous:
                        combined[cname] = None
                out.append(combined)
        return out

    def _inner_candidates(self, join, inner_schema, outer, txn, drive):
        if drive is not None:
            val = outer.get(drive)
            if isinstance(val, int) and not isinstance(val, bool):
                plan = planner.Plan(join.table, planner.INDEX_SEEK, inner_schema.pk,
                                    seek_key=val, residual=None)
                return [row for _, row, _ in self._scan(inner_schema, plan, txn)]
            return []  # a non-int value cannot equal an INT primary key
        plan = planner.Plan(join.table, planner.SEQ_SCAN, inner_schema.pk, residual=None)
        return [row for _, row, _ in self._scan(inner_schema, plan, txn)]

    def _equijoin_drive(self, join, inner_schema, key_of):
        """If ON contains ``<outer> = <inner.pk>``, return the outer side's key."""
        for c in _flatten_and(join.on):
            if not (isinstance(c, ast.BinOp) and c.op == "="):
                continue
            for a, b in ((c.left, c.right), (c.right, c.left)):
                if (isinstance(a, ast.Column) and isinstance(b, ast.Column)
                        and self._is_inner_pk(a, join.table, inner_schema)
                        and not self._is_inner_pk(b, join.table, inner_schema)):
                    return key_of(b)
        return None

    @staticmethod
    def _is_inner_pk(col, inner_table, inner_schema):
        if col.name != inner_schema.pk:
            return False
        return col.table == inner_table or col.table is None

    @staticmethod
    def _projection(items, table_list, ambiguous):
        schemas = {t: s for t, s in table_list}
        out = []
        for item in items:
            if item.name == "*" and item.table is None:
                for t, s in table_list:
                    for cname, _ in s.columns:
                        label = cname if cname not in ambiguous else f"{t}.{cname}"
                        out.append((label, f"{t}.{cname}"))
            elif item.name == "*":  # table.*
                for cname, _ in schemas[item.table].columns:
                    label = cname if cname not in ambiguous else f"{item.table}.{cname}"
                    out.append((label, f"{item.table}.{cname}"))
            else:
                key = item.name if item.table is None else f"{item.table}.{item.name}"
                out.append((item.name, key))
        return out

    def _explain_rows(self, stmt, table_list, base_plan, key_of):
        if not stmt.joins:
            return [[base_plan.describe()]]
        schemas = {t: s for t, s in table_list}
        rows = [["Nested Loop Join"], ["  -> " + base_plan.describe()]]
        for j in stmt.joins:
            inner_schema = schemas[j.table]
            drive = self._equijoin_drive(j, inner_schema, key_of)
            if drive is not None:
                access = (f"Index Seek on {j.table}_pkey "
                          f"({j.table}.{inner_schema.pk} = {drive}) [per outer row]")
            else:
                access = f"Seq Scan on {j.table} [per outer row]"
            rows.append([f"  -> {j.kind} Join  {access}"])
        return rows

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
