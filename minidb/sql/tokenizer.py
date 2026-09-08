"""A small hand-written tokenizer for the SQL subset minidb understands."""

from __future__ import annotations

from dataclasses import dataclass

KEYWORDS = {
    "create", "table", "drop", "insert", "into", "values", "select", "from",
    "where", "update", "set", "delete", "begin", "commit", "rollback", "and",
    "or", "not", "null", "primary", "key", "int", "integer", "text", "float",
    "order", "by", "asc", "desc", "limit", "true", "false", "explain",
    "join", "inner", "left", "right", "outer", "on", "group", "having",
    "index", "unique",
}

_SYMBOLS = {
    "(", ")", ",", ";", "*", "=", "<", ">", "<=", ">=", "!=", "<>", ".", "?",
}


@dataclass
class Token:
    kind: str  # keyword | ident | number | string | symbol | eof
    value: str
    pos: int


class TokenizeError(ValueError):
    pass


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":  # line comment
            while i < n and sql[i] != "\n":
                i += 1
            continue
        start = i
        # string literal (single quotes; '' escapes a quote)
        if c == "'":
            i += 1
            buf = []
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(sql[i])
                i += 1
            else:
                raise TokenizeError("unterminated string literal")
            tokens.append(Token("string", "".join(buf), start))
            continue
        # number (int or float)
        if c.isdigit() or (c == "-" and i + 1 < n and sql[i + 1].isdigit()
                           and (not tokens or tokens[-1].kind in ("symbol", "keyword")
                                and tokens[-1].value not in (")",))):
            i += 1
            seen_dot = False
            while i < n and (sql[i].isdigit() or (sql[i] == "." and not seen_dot)):
                if sql[i] == ".":
                    seen_dot = True
                i += 1
            tokens.append(Token("number", sql[start:i], start))
            continue
        # identifier / keyword
        if c.isalpha() or c == "_":
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            kind = "keyword" if word.lower() in KEYWORDS else "ident"
            tokens.append(Token(kind, word, start))
            continue
        # two-char symbols first
        two = sql[i : i + 2]
        if two in _SYMBOLS:
            tokens.append(Token("symbol", two, start))
            i += 2
            continue
        if c in _SYMBOLS:
            tokens.append(Token("symbol", c, start))
            i += 1
            continue
        raise TokenizeError(f"unexpected character {c!r} at {i}")
    tokens.append(Token("eof", "", n))
    return tokens
