"""AST node definitions produced by the parser and consumed by the planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# -- expressions -----------------------------------------------------------
@dataclass
class Column:
    """A column reference. ``table`` is the optional qualifier in ``t.col``.

    ``name == "*"`` represents a star: ``*`` (table is None) or ``t.*``.
    """

    name: str
    table: Optional[str] = None


@dataclass
class Literal:
    value: Any  # int | float | str | None | bool


@dataclass
class Parameter:
    """A ``?`` placeholder, bound positionally from the params sequence."""

    index: int


@dataclass
class Aggregate:
    """An aggregate function call, e.g. ``COUNT(*)`` or ``SUM(amount)``."""

    func: str  # COUNT | SUM | AVG | MIN | MAX
    column: "Column"  # name may be "*" (only valid for COUNT)

    @property
    def label(self) -> str:
        arg = "*" if self.column.name == "*" else (
            self.column.name if self.column.table is None
            else f"{self.column.table}.{self.column.name}")
        return f"{self.func.lower()}({arg})"


@dataclass
class BinOp:
    op: str  # = != < <= > >= AND OR NOT
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
class CreateIndex:
    name: str
    table: str
    columns: list[str]
    unique: bool = False


@dataclass
class DropIndex:
    name: str


@dataclass
class Insert:
    table: str
    columns: Optional[list[str]]
    rows: list[list[Any]]  # each value is a Literal


@dataclass
class OrderBy:
    column: Column
    descending: bool = False


@dataclass
class Join:
    table: str
    on: Any  # a predicate expression evaluated against the joined row
    kind: str = "INNER"  # INNER | LEFT


@dataclass
class Select:
    table: str  # the base (left-most) table in FROM
    columns: list[Any]  # Column or Aggregate items (Column.name may be "*")
    joins: list[Join] = field(default_factory=list)
    where: Optional[Any] = None
    group_by: list[Column] = field(default_factory=list)
    having: Optional[Any] = None
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
