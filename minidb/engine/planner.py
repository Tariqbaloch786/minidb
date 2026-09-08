"""A tiny cost-unaware planner that chooses an access path for SELECT/UPDATE/DELETE.

The one real optimization: if the ``WHERE`` clause constrains the primary key
(which is also the B+Tree key), we turn it into an **index seek** (point lookup)
or an **index range scan** instead of a full **sequential scan**. Any leftover
predicate is kept as a *residual filter* applied to the rows the access path
returns. ``EXPLAIN`` prints the resulting plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..sql import ast

SEQ_SCAN = "SeqScan"
INDEX_SEEK = "IndexSeek"
INDEX_RANGE = "IndexRange"
INDEX_SCAN = "IndexScan"  # secondary (non-PK) index


@dataclass
class Plan:
    table: str
    method: str
    pk_column: str
    seek_key: Optional[int] = None
    lo: Optional[int] = None
    hi: Optional[int] = None
    residual: Optional[Any] = None  # WHERE predicate still to evaluate per row
    # secondary-index scan (method == INDEX_SCAN); bounds are encoded key bytes,
    # index_hi is exclusive. The executor fills these in.
    index_name: Optional[str] = None
    index_root: Optional[int] = None
    index_lo: Optional[bytes] = None
    index_hi: Optional[bytes] = None
    index_desc: str = ""

    def describe(self) -> str:
        if self.method == INDEX_SEEK:
            access = f"Index Seek on {self.table}_pkey ({self.pk_column} = {self.seek_key})"
        elif self.method == INDEX_RANGE:
            lo = "-inf" if self.lo is None else self.lo
            hi = "+inf" if self.hi is None else self.hi
            access = f"Index Range Scan on {self.table}_pkey ({lo} <= {self.pk_column} <= {hi})"
        elif self.method == INDEX_SCAN:
            access = f"Index Scan on {self.index_name} ({self.index_desc})"
        else:
            access = f"Seq Scan on {self.table}"
        if self.residual is not None:
            access += "  [filter]"
        return access


def _flatten_and(expr: Any) -> list[Any]:
    if isinstance(expr, ast.BinOp) and expr.op == "AND":
        return _flatten_and(expr.left) + _flatten_and(expr.right)
    return [expr]


def _rebuild_and(conjuncts: list[Any]) -> Optional[Any]:
    if not conjuncts:
        return None
    node = conjuncts[0]
    for c in conjuncts[1:]:
        node = ast.BinOp("AND", node, c)
    return node


def plan_scan(table: str, pk_column: str, where: Optional[Any]) -> Plan:
    if where is None:
        return Plan(table, SEQ_SCAN, pk_column, residual=None)

    # OR at the top can't be satisfied by a single index range safely.
    if isinstance(where, ast.BinOp) and where.op == "OR":
        return Plan(table, SEQ_SCAN, pk_column, residual=where)

    conjuncts = _flatten_and(where)
    lo: Optional[int] = None
    hi: Optional[int] = None
    seek_key: Optional[int] = None
    consumed: list[int] = []

    for i, c in enumerate(conjuncts):
        if not (isinstance(c, ast.BinOp) and c.op in {"=", "<", "<=", ">", ">="}):
            continue
        left, right = c.left, c.right
        if not (isinstance(left, ast.Column) and left.name == pk_column):
            continue
        if not (isinstance(right, ast.Literal) and isinstance(right.value, int)
                and not isinstance(right.value, bool)):
            continue
        v = right.value
        if c.op == "=":
            seek_key = v
        elif c.op == ">":
            lo = v + 1 if lo is None else max(lo, v + 1)
        elif c.op == ">=":
            lo = v if lo is None else max(lo, v)
        elif c.op == "<":
            hi = v - 1 if hi is None else min(hi, v - 1)
        elif c.op == "<=":
            hi = v if hi is None else min(hi, v)
        consumed.append(i)

    residual = _rebuild_and([c for i, c in enumerate(conjuncts) if i not in consumed])

    if seek_key is not None:
        return Plan(table, INDEX_SEEK, pk_column, seek_key=seek_key, residual=residual)
    if lo is not None or hi is not None:
        return Plan(table, INDEX_RANGE, pk_column, lo=lo, hi=hi, residual=residual)
    return Plan(table, SEQ_SCAN, pk_column, residual=where)
