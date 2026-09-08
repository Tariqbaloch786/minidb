"""A threaded TCP server so several clients can share one minidb database.

This is what turns minidb from an embedded library into something a team or a
service can run centrally. The model is deliberately SQLite-like: every request
is executed under a single global lock, so access is **serialized** and always
consistent. Reads see an MVCC snapshot; writes take turns.

Transactions are per-connection: a connection that issues ``BEGIN`` owns the
write path until it ``COMMIT``s or ``ROLLBACK``s (or disconnects, which rolls
back). While one connection holds a transaction, others attempting to write are
told the database is locked — the same contract SQLite offers.

Wire protocol: newline-delimited JSON. Request ``{"sql": "...", "params": []}``;
response ``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``.

Run it::

    python -m minidb.server data.db --host 127.0.0.1 --port 4321
    minidb-server data.db            # installed console script
"""

from __future__ import annotations

import argparse
import json
import socketserver
import threading

from .database import Database


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        conn_id = id(self)
        server: "Server" = self.server  # type: ignore[assignment]
        for raw in self.rfile:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except ValueError:
                self._send({"ok": False, "error": "invalid JSON request"})
                continue
            self._send(server.handle_request(conn_id, req))

    def finish(self) -> None:
        try:
            self.server.cleanup(id(self))  # type: ignore[attr-defined]
        finally:
            super().finish()

    def _send(self, obj: dict) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, path: str = ":memory:", host: str = "127.0.0.1", port: int = 4321):
        super().__init__((host, port), _Handler)
        self.db = Database(path)
        self.lock = threading.RLock()
        self.txn_owner = None  # id of the connection currently in a transaction

    # -- request handling (all under the global lock) ----------------------
    def handle_request(self, conn_id: int, req: dict) -> dict:
        sql = req.get("sql")
        params = tuple(req.get("params") or [])
        if not isinstance(sql, str) or not sql.strip():
            return {"ok": False, "error": "missing 'sql'"}
        with self.lock:
            try:
                result = self._exec(conn_id, sql, params)
            except Exception as exc:  # noqa: BLE001 - report to the client
                return {"ok": False, "error": str(exc)}
            return _to_response(result)

    def _exec(self, conn_id: int, sql: str, params: tuple):
        head = sql.lstrip()[:9].upper()
        if head.startswith("BEGIN"):
            if self.txn_owner not in (None, conn_id):
                raise RuntimeError("database is locked by another connection's transaction")
            result = self.db.execute(sql, params)
            self.txn_owner = conn_id
            return result
        if head.startswith(("COMMIT", "ROLLBACK")):
            if self.txn_owner != conn_id:
                raise RuntimeError("no transaction owned by this connection")
            result = self.db.execute(sql, params)
            self.txn_owner = None
            return result
        # data statement
        if self.txn_owner not in (None, conn_id):
            raise RuntimeError("database is locked by another connection's transaction")
        return self.db.execute(sql, params)

    def cleanup(self, conn_id: int) -> None:
        with self.lock:
            if self.txn_owner == conn_id:
                try:
                    self.db.execute("ROLLBACK")
                except Exception:  # noqa: BLE001 - best-effort
                    pass
                self.txn_owner = None

    def server_close(self) -> None:
        super().server_close()
        self.db.close()


def _to_response(result) -> dict:
    if result.kind in ("select", "explain"):
        return {"ok": True, "kind": result.kind,
                "columns": result.columns, "rows": result.rows}
    return {"ok": True, "kind": result.kind,
            "rowcount": result.rowcount, "message": result.message}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="minidb TCP server")
    parser.add_argument("path", nargs="?", default=":memory:",
                        help="database file (default: in-memory)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4321)
    args = parser.parse_args(argv)

    server = Server(args.path, args.host, args.port)
    print(f"minidb server listening on {args.host}:{args.port}  (db={args.path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        print("\nshutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
