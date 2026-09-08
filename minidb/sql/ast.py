"""AST node definitions produced by the parser and consumed by the planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# -- expressions -----------------------------------------------------------
@dataclass
class Column:
    name: str


@dataclass
class Literal:
    value: Any  # int | float | str | None | bool


@dataclass
class BinOp:
    op: str  # = != < <= > >= AND OR
    left: Any
    right: Any


# -- column / table definitions -------------------------------------------
@dataclass
class ColumnDef:
    name: str
    type: str  # INT | TEXT | FLOAT
    primary_key: bool = False
    not_null: bool = False


# -- statements ------------------------------------------------------------
@dataclass
class CreateTable:
    name: str
    columns: list[ColumnDef]


@dataclass
class DropTable:
    name: str


@dataclass
class Insert:
    table: str
    columns: Optional[list[str]]
    rows: list[list[Any]]  # each value is a Literal


@dataclass
class OrderBy:
    column: str
    descending: bool = False


@dataclass
class Select:
    table: str
    columns: list[str]  # ['*'] or explicit column names
    where: Optional[Any] = None
    order_by: Optional[OrderBy] = None
    limit: Optional[int] = None
    explain: bool = False


@dataclass
class Update:
    table: str
    assignments: list[tuple[str, Any]]  # (column, Literal)
    where: Optional[Any] = None


@dataclass
class Delete:
    table: str
    where: Optional[Any] = None


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass
