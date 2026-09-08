"""A tiny client for the minidb TCP server (see :mod:`minidb.server`).

>>> from minidb.client import connect
>>> c = connect("127.0.0.1", 4321)
>>> c.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
>>> c.execute("INSERT INTO t VALUES (?, ?)", (1, "ada"))
>>> c.execute("SELECT * FROM t")["rows"]
[[1, 'ada']]
>>> c.close()

The wire protocol is newline-delimited JSON, so a client in any language is a
few lines of code; this one just makes it convenient from Python.
"""

from __future__ import annotations

import json
import socket
from typing import Any


class ServerError(RuntimeError):
    """Raised when the server reports ``ok: false`` for a request."""


class Client:
    def __init__(self, host: str = "127.0.0.1", port: int = 4321):
        self._sock = socket.create_connection((host, port))
        self._f = self._sock.makefile("rwb")

    def execute(self, sql: str, params: tuple = ()) -> dict[str, Any]:
        """Run one statement; return the decoded response dict.

        For a SELECT the dict has ``columns`` and ``rows``; for DML it has
        ``rowcount`` and ``message``. Raises :class:`ServerError` on failure.
        """
        request = {"sql": sql, "params": list(params)}
        self._f.write((json.dumps(request) + "\n").encode("utf-8"))
        self._f.flush()
        line = self._f.readline()
        if not line:
            raise ServerError("server closed the connection")
        response = json.loads(line)
        if not response.get("ok"):
            raise ServerError(response.get("error", "unknown server error"))
        return response

    def close(self) -> None:
        try:
            self._f.close()
        finally:
            self._sock.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(host: str = "127.0.0.1", port: int = 4321) -> Client:
    return Client(host, port)
