"""A recursive-descent parser for minidb's SQL subset.

Supported grammar (informally)::

    CREATE TABLE t (col TYPE [PRIMARY KEY] [NOT NULL], ...)
    DROP TABLE t
    INSERT INTO t [(cols)] VALUES (v, ...), (v, ...)
    SELECT */cols FROM t [WHERE expr] [ORDER BY col [ASC|DESC]] [LIMIT n]
    UPDATE t SET col = v, ... [WHERE expr]
    DELETE FROM t [WHERE expr]
    BEGIN | COMMIT | ROLLBACK
    EXPLAIN <select>

Expressions support =, !=, <, <=, >, >=, AND, OR, NOT and parentheses.
"""

from __future__ import annotations

from typing import Any, Optional

from . import ast
from .tokenizer import Token, tokenize


class ParseError(ValueError):
    pass


_COMPARATORS = {"=", "!=", "<>", "<", "<=", ">", ">="}


_AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


class Parser:
    def __init__(self, sql: str):
        self.tokens = tokenize(sql)
        self.pos = 0
        self._param_count = 0

    # -- token helpers -----------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def at_kw(self, *words: str) -> bool:
        t = self.cur
        return t.kind == "keyword" and t.value.lower() in words

    def at_sym(self, *syms: str) -> bool:
        t = self.cur
        return t.kind == "symbol" and t.value in syms

    def expect_kw(self, word: str) -> Token:
        if not self.at_kw(word):
            raise ParseError(f"expected {word.upper()}, got {self.cur.value!r}")
        return self.advance()

    def expect_sym(self, sym: str) -> Token:
        if not self.at_sym(sym):
            raise ParseError(f"expected {sym!r}, got {self.cur.value!r}")
        return self.advance()

    def expect_ident(self) -> str:
        if self.cur.kind != "ident":
            raise ParseError(f"expected identifier, got {self.cur.value!r}")
        return self.advance().value

    # -- entry point -------------------------------------------------------
    def parse(self) -> Any:
        if self.at_kw("explain"):
            self.advance()
            stmt = self._select()
            stmt.explain = True
            node = stmt
        elif self.at_kw("create"):
            node = self._create()
        elif self.at_kw("drop"):
            node = self._drop()
        elif self.at_kw("insert"):
            node = self._insert()
        elif self.at_kw("select"):
            node = self._select()
        elif self.at_kw("update"):
            node = self._update()
        elif self.at_kw("delete"):
            node = self._delete()
        elif self.at_kw("begin"):
            self.advance()
            node = ast.Begin()
        elif self.at_kw("commit"):
            self.advance()
            node = ast.Commit()
        elif self.at_kw("rollback"):
            self.advance()
            node = ast.Rollback()
        else:
            raise ParseError(f"unexpected token {self.cur.value!r}")
        if self.at_sym(";"):
            self.advance()
        if self.cur.kind != "eof":
            raise ParseError(f"trailing tokens after statement: {self.cur.value!r}")
        return node

    # -- statements --------------------------------------------------------
    def _create(self):
        self.expect_kw("create")
        if self.at_kw("unique", "index"):
            return self._create_index()
        self.expect_kw("table")
        name = self.expect_ident()
        self.expect_sym("(")
        columns: list[ast.ColumnDef] = []
        while True:
            col_name = self.expect_ident()
            col_type = self._type()
            pk = False
            not_null = False
            while self.at_kw("primary", "not"):
                if self.at_kw("primary"):
                    self.advance()
                    self.expect_kw("key")
                    pk = True
                    not_null = True
                else:
                    self.advance()
                    self.expect_kw("null")
                    not_null = True
            columns.append(ast.ColumnDef(col_name, col_type, pk, not_null))
            if self.at_sym(","):
                self.advance()
                continue
            break
        self.expect_sym(")")
        if sum(c.primary_key for c in columns) != 1:
            raise ParseError("a table needs exactly one PRIMARY KEY column")
        return ast.CreateTable(name, columns)

    def _type(self) -> str:
        if self.at_kw("int", "integer"):
            self.advance()
            return "INT"
        if self.at_kw("text"):
            self.advance()
            return "TEXT"
        if self.at_kw("float"):
            self.advance()
            return "FLOAT"
        raise ParseError(f"unknown type {self.cur.value!r}")

    def _create_index(self) -> ast.CreateIndex:
        unique = False
        if self.at_kw("unique"):
            self.advance()
            unique = True
        self.expect_kw("index")
        name = self.expect_ident()
        self.expect_kw("on")
        table = self.expect_ident()
        self.expect_sym("(")
        columns = [self.expect_ident()]
        while self.at_sym(","):
            self.advance()
            columns.append(self.expect_ident())
        self.expect_sym(")")
        return ast.CreateIndex(name, table, columns, unique)

    def _drop(self):
        self.expect_kw("drop")
        if self.at_kw("index"):
            self.advance()
            return ast.DropIndex(self.expect_ident())
        self.expect_kw("table")
        return ast.DropTable(self.expect_ident())

    def _insert(self) -> ast.Insert:
        self.expect_kw("insert")
        self.expect_kw("into")
        table = self.expect_ident()
        columns: Optional[list[str]] = None
        if self.at_sym("("):
            self.advance()
            columns = [self.expect_ident()]
            while self.at_sym(","):
                self.advance()
                columns.append(self.expect_ident())
            self.expect_sym(")")
        self.expect_kw("values")
        rows: list[list[Any]] = []
        while True:
            self.expect_sym("(")
            values = [self._literal()]
            while self.at_sym(","):
                self.advance()
                values.append(self._literal())
            self.expect_sym(")")
            rows.append(values)
            if self.at_sym(","):
                self.advance()
                continue
            break
        return ast.Insert(table, columns, rows)

    def _select(self) -> ast.Select:
        self.expect_kw("select")
        columns = [self._select_item()]
        while self.at_sym(","):
            self.advance()
            columns.append(self._select_item())
        self.expect_kw("from")
        table = self.expect_ident()
        joins = []
        while self.at_kw("join", "inner", "left", "right"):
            joins.append(self._join())
        where = None
        if self.at_kw("where"):
            self.advance()
            where = self._expr()
        group_by = []
        if self.at_kw("group"):
            self.advance()
            self.expect_kw("by")
            group_by = [self._column_ref()]
            while self.at_sym(","):
                self.advance()
                group_by.append(self._column_ref())
        having = None
        if self.at_kw("having"):
            self.advance()
            having = self._expr()
        order_by = None
        if self.at_kw("order"):
            self.advance()
            self.expect_kw("by")
            col = self._agg_or_column()
            desc = False
            if self.at_kw("asc"):
                self.advance()
            elif self.at_kw("desc"):
                self.advance()
                desc = True
            order_by = ast.OrderBy(col, desc)
        limit = None
        if self.at_kw("limit"):
            self.advance()
            if self.cur.kind != "number":
                raise ParseError("LIMIT expects a number")
            limit = int(self.advance().value)
        return ast.Select(table, columns, joins=joins, where=where,
                          group_by=group_by, having=having,
                          order_by=order_by, limit=limit)

    def _select_item(self):
        agg = self._maybe_aggregate()
        if agg is not None:
            return agg
        if self.at_sym("*"):
            self.advance()
            return ast.Column("*", None)
        first = self.expect_ident()
        if self.at_sym("."):
            self.advance()
            if self.at_sym("*"):
                self.advance()
                return ast.Column("*", first)
            return ast.Column(self.expect_ident(), first)
        return ast.Column(first, None)

    def _maybe_aggregate(self):
        t = self.cur
        nxt = self.tokens[self.pos + 1]
        if (t.kind == "ident" and t.value.upper() in _AGG_FUNCS
                and nxt.kind == "symbol" and nxt.value == "("):
            func = self.advance().value.upper()
            self.expect_sym("(")
            if self.at_sym("*"):
                self.advance()
                col = ast.Column("*", None)
            else:
                col = self._column_ref()
            self.expect_sym(")")
            if func != "COUNT" and col.name == "*":
                raise ParseError(f"{func}(*) is not allowed; use {func}(column)")
            return ast.Aggregate(func, col)
        return None

    def _join(self) -> ast.Join:
        kind = "INNER"
        if self.at_kw("inner"):
            self.advance()
        elif self.at_kw("left"):
            self.advance()
            if self.at_kw("outer"):
                self.advance()
            kind = "LEFT"
        elif self.at_kw("right"):
            raise ParseError("RIGHT JOIN is not supported yet")
        self.expect_kw("join")
        table = self.expect_ident()
        self.expect_kw("on")
        on = self._expr()
        return ast.Join(table, on, kind)

    def _update(self) -> ast.Update:
        self.expect_kw("update")
        table = self.expect_ident()
        self.expect_kw("set")
        assignments = [self._assignment()]
        while self.at_sym(","):
            self.advance()
            assignments.append(self._assignment())
        where = None
        if self.at_kw("where"):
            self.advance()
            where = self._expr()
        return ast.Update(table, assignments, where)

    def _assignment(self) -> tuple[str, ast.Literal]:
        col = self.expect_ident()
        self.expect_sym("=")
        return (col, self._literal())

    def _delete(self) -> ast.Delete:
        self.expect_kw("delete")
        self.expect_kw("from")
        table = self.expect_ident()
        where = None
        if self.at_kw("where"):
            self.advance()
            where = self._expr()
        return ast.Delete(table, where)

    # -- expressions -------------------------------------------------------
    def _expr(self) -> Any:
        return self._or()

    def _or(self) -> Any:
        left = self._and()
        while self.at_kw("or"):
            self.advance()
            left = ast.BinOp("OR", left, self._and())
        return left

    def _and(self) -> Any:
        left = self._not()
        while self.at_kw("and"):
            self.advance()
            left = ast.BinOp("AND", left, self._not())
        return left

    def _not(self) -> Any:
        if self.at_kw("not"):
            self.advance()
            # NOT x  ==  x != TRUE-ish; we model it as (x = FALSE) style negation
            return ast.BinOp("NOT", self._not(), ast.Literal(None))
        return self._comparison()

    def _comparison(self) -> Any:
        if self.at_sym("("):
            self.advance()
            inner = self._expr()
            self.expect_sym(")")
            return inner
        left = self._operand()
        if not (self.cur.kind == "symbol" and self.cur.value in _COMPARATORS):
            raise ParseError(f"expected comparator, got {self.cur.value!r}")
        op = self.advance().value
        if op == "<>":
            op = "!="
        right = self._operand()
        return ast.BinOp(op, left, right)

    def _operand(self) -> Any:
        """One side of a comparison: an aggregate, column reference, or literal."""
        agg = self._maybe_aggregate()
        if agg is not None:
            return agg
        if self.cur.kind == "ident":
            return self._column_ref()
        return self._literal()

    def _column_ref(self) -> ast.Column:
        first = self.expect_ident()
        if self.at_sym("."):
            self.advance()
            return ast.Column(self.expect_ident(), first)
        return ast.Column(first, None)

    def _agg_or_column(self):
        agg = self._maybe_aggregate()
        return agg if agg is not None else self._column_ref()

    def _literal(self):
        t = self.cur
        if self.at_sym("?"):
            self.advance()
            idx = self._param_count
            self._param_count += 1
            return ast.Parameter(idx)
        if t.kind == "number":
            self.advance()
            return ast.Literal(float(t.value) if "." in t.value else int(t.value))
        if t.kind == "string":
            self.advance()
            return ast.Literal(t.value)
        if self.at_kw("null"):
            self.advance()
            return ast.Literal(None)
        if self.at_kw("true"):
            self.advance()
            return ast.Literal(True)
        if self.at_kw("false"):
            self.advance()
            return ast.Literal(False)
        raise ParseError(f"expected a literal, got {t.value!r}")


def parse(sql: str) -> Any:
    return Parser(sql).parse()


# -- parameter binding -----------------------------------------------------
def _sub_expr(expr: Any, params) -> Any:
    if isinstance(expr, ast.Parameter):
        if expr.index >= len(params):
            raise ParseError("not enough parameters supplied for placeholders")
        return ast.Literal(params[expr.index])
    if isinstance(expr, ast.BinOp):
        expr.left = _sub_expr(expr.left, params)
        expr.right = _sub_expr(expr.right, params)
    return expr


def count_parameters(stmt: Any) -> int:
    """Number of ``?`` placeholders in a parsed statement."""
    total = 0

    def walk(expr):
        nonlocal total
        if isinstance(expr, ast.Parameter):
            total += 1
        elif isinstance(expr, ast.BinOp):
            walk(expr.left)
            walk(expr.right)

    if isinstance(stmt, ast.Insert):
        for row in stmt.rows:
            for v in row:
                walk(v)
    elif isinstance(stmt, ast.Update):
        for _, v in stmt.assignments:
            walk(v)
        walk(stmt.where)
    elif isinstance(stmt, ast.Select):
        walk(stmt.where)
        walk(stmt.having)
        for j in stmt.joins:
            walk(j.on)
    elif isinstance(stmt, ast.Delete):
        walk(stmt.where)
    return total


def bind_parameters(stmt: Any, params) -> Any:
    """Replace ``?`` placeholders in ``stmt`` with literal values from ``params``.

    Parameterized queries are the safe way to pass user data into SQL: values
    never touch the SQL text, so there is no SQL-injection surface.
    """
    expected = count_parameters(stmt)
    if len(params) != expected:
        raise ParseError(
            f"query has {expected} placeholder(s) but {len(params)} parameter(s) given")
    if isinstance(stmt, ast.Insert):
        stmt.rows = [[_sub_expr(v, params) for v in row] for row in stmt.rows]
    elif isinstance(stmt, ast.Update):
        stmt.assignments = [(c, _sub_expr(v, params)) for c, v in stmt.assignments]
        if stmt.where is not None:
            stmt.where = _sub_expr(stmt.where, params)
    elif isinstance(stmt, ast.Select):
        if stmt.where is not None:
            stmt.where = _sub_expr(stmt.where, params)
        if stmt.having is not None:
            stmt.having = _sub_expr(stmt.having, params)
        for j in stmt.joins:
            j.on = _sub_expr(j.on, params)
    elif isinstance(stmt, ast.Delete):
        if stmt.where is not None:
            stmt.where = _sub_expr(stmt.where, params)
    return stmt
