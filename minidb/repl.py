"""An interactive shell for minidb, à la ``sqlite3``.

Run it with::

    python -m minidb              # in-memory
    python -m minidb mydata.db    # persistent file

Dot-commands:

    .tables            list tables
    .schema [table]    show CREATE-TABLE-like schema
    .help              show help
    .exit / .quit      leave
"""

from __future__ import annotations

import sys
import time

from .database import Database
from .engine.executor import Result


def _render_table(columns: list[str], rows: list[list]) -> str:
    if not columns:
        return ""
    cells = [[_fmt(v) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    def line(parts):
        return " | ".join(p.ljust(widths[i]) for i, p in enumerate(parts))

    sep = "-+-".join("-" * w for w in widths)
    out = [line(columns), sep]
    out += [line(row) for row in cells]
    return "\n".join(out)


def _fmt(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _print_result(result: Result, elapsed_ms: float) -> None:
    if result.kind in ("select", "explain"):
        if result.rows:
            print(_render_table(result.columns, result.rows))
        print(f"({len(result.rows)} row{'s' if len(result.rows) != 1 else ''}, "
              f"{elapsed_ms:.2f}ms)")
    elif result.kind == "dml":
        print(f"{result.message}  ({elapsed_ms:.2f}ms)")
    else:
        print(result.message)


def _dot_command(db: Database, line: str) -> bool:
    """Handle a .command. Returns False to signal exit."""
    parts = line.split()
    cmd = parts[0].lower()
    if cmd in (".exit", ".quit"):
        return False
    if cmd == ".help":
        print(__doc__)
    elif cmd == ".tables":
        for name in sorted(db.catalog.tables):
            print(name)
    elif cmd == ".schema":
        wanted = parts[1] if len(parts) > 1 else None
        for name, schema in sorted(db.catalog.tables.items()):
            if wanted and name != wanted:
                continue
            cols = []
            for cname, ctype in schema.columns:
                bits = f"{cname} {ctype}"
                if cname == schema.pk:
                    bits += " PRIMARY KEY"
                elif cname in schema.not_null:
                    bits += " NOT NULL"
                cols.append(bits)
            print(f"CREATE TABLE {name} (" + ", ".join(cols) + ");")
    else:
        print(f"unknown command: {cmd}")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = argv[0] if argv else ":memory:"
    db = Database(path)
    where = "in-memory" if path == ":memory:" else path
    print(f"minidb - connected to {where}")
    print("Type SQL statements, or .help for commands. .exit to quit.")

    buffer = ""
    try:
        while True:
            prompt = "minidb> " if not buffer else "   ...> "
            try:
                line = input(prompt)
            except EOFError:
                print()
                break

            stripped = line.strip()
            if not buffer and stripped.startswith("."):
                if not _dot_command(db, stripped):
                    break
                continue

            buffer += line + "\n"
            if ";" not in line:
                continue

            sql = buffer.strip().rstrip(";")
            buffer = ""
            if not sql:
                continue
            try:
                start = time.perf_counter()
                result = db.execute(sql)
                _print_result(result, (time.perf_counter() - start) * 1000)
            except Exception as exc:  # keep the REPL alive on errors
                print(f"Error: {exc}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
