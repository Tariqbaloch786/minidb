"""Tests for the tokenizer and parser."""

import pytest

from minidb.sql import ast
from minidb.sql.parser import ParseError, parse
from minidb.sql.tokenizer import tokenize


def test_tokenize_basic():
    kinds = [t.kind for t in tokenize("SELECT * FROM t WHERE id = 1;")]
    assert kinds == [
        "keyword", "symbol", "keyword", "ident", "keyword",
        "ident", "symbol", "number", "symbol", "eof",
    ]


def test_string_literal_with_escape():
    toks = tokenize("'it''s ok'")
    assert toks[0].kind == "string"
    assert toks[0].value == "it's ok"


def test_parse_create_table():
    stmt = parse("CREATE TABLE t (id INT PRIMARY KEY, name TEXT NOT NULL)")
    assert isinstance(stmt, ast.CreateTable)
    assert stmt.name == "t"
    assert stmt.columns[0].primary_key
    assert stmt.columns[1].not_null


def test_create_table_requires_single_pk():
    with pytest.raises(ParseError):
        parse("CREATE TABLE t (a INT, b TEXT)")
    with pytest.raises(ParseError):
        parse("CREATE TABLE t (a INT PRIMARY KEY, b INT PRIMARY KEY)")


def test_parse_insert_multirow():
    stmt = parse("INSERT INTO t (a, b) VALUES (1, 'x'), (2, 'y')")
    assert isinstance(stmt, ast.Insert)
    assert stmt.columns == ["a", "b"]
    assert len(stmt.rows) == 2
    assert stmt.rows[1][0].value == 2


def test_parse_select_full():
    stmt = parse("SELECT a, b FROM t WHERE a >= 1 AND b < 5 ORDER BY a DESC LIMIT 10")
    assert stmt.columns == ["a", "b"]
    assert stmt.order_by.descending
    assert stmt.limit == 10
    assert isinstance(stmt.where, ast.BinOp)
    assert stmt.where.op == "AND"


def test_parse_negative_and_float_numbers():
    stmt = parse("INSERT INTO t VALUES (-5, 3.14)")
    assert stmt.rows[0][0].value == -5
    assert stmt.rows[0][1].value == 3.14


def test_explain_flag():
    stmt = parse("EXPLAIN SELECT * FROM t WHERE id = 1")
    assert isinstance(stmt, ast.Select)
    assert stmt.explain


def test_trailing_tokens_error():
    with pytest.raises(ParseError):
        parse("SELECT * FROM t garbage")
