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


class Parser:
    def __init__(self, sql: str):
        self.tokens = tokenize(sql)
        self.pos = 0

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
    def _create(self) -> ast.CreateTable:
        self.expect_kw("create")
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

    def _drop(self) -> ast.DropTable:
        self.expect_kw("drop")
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
        order_by = None
        if self.at_kw("order"):
            self.advance()
            self.expect_kw("by")
            col = self._column_ref()
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
        return ast.Select(table, columns, joins, where, order_by, limit)

    def _select_item(self) -> ast.Column:
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
        """One side of a comparison: a column reference or a literal."""
        if self.cur.kind == "ident":
            return self._column_ref()
        return self._literal()

    def _column_ref(self) -> ast.Column:
        first = self.expect_ident()
        if self.at_sym("."):
            self.advance()
            return ast.Column(self.expect_ident(), first)
        return ast.Column(first, None)

    def _literal(self) -> ast.Literal:
        t = self.cur
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
