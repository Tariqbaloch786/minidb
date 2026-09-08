"""Tests for the TCP server and its client."""

import threading
import time

import pytest

from minidb.client import ServerError, connect
from minidb.server import Server


@pytest.fixture
def server(tmp_path):
    srv = Server(str(tmp_path / "srv.db"), "127.0.0.1", 0)  # port 0 -> ephemeral
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield port
    srv.shutdown()
    srv.server_close()


def test_client_roundtrip(server):
    c = connect("127.0.0.1", server)
    c.execute("CREATE TABLE t (id INT PRIMARY KEY, name TEXT)")
    c.execute("INSERT INTO t VALUES (?, ?)", (1, "ada"))
    resp = c.execute("SELECT * FROM t")
    assert resp["columns"] == ["id", "name"]
    assert resp["rows"] == [[1, "ada"]]
    c.close()


def test_two_clients_share_one_database(server):
    a = connect("127.0.0.1", server)
    b = connect("127.0.0.1", server)
    a.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    a.execute("INSERT INTO t VALUES (1, 'hello')")
    assert b.execute("SELECT v FROM t")["rows"] == [["hello"]]
    a.close()
    b.close()


def test_transaction_locks_out_other_writers(server):
    a = connect("127.0.0.1", server)
    b = connect("127.0.0.1", server)
    a.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")

    a.execute("BEGIN")
    a.execute("INSERT INTO t VALUES (1, 'a')")
    with pytest.raises(ServerError):
        b.execute("INSERT INTO t VALUES (2, 'b')")  # database is locked by A
    a.execute("COMMIT")

    # after A commits, B can write
    assert b.execute("INSERT INTO t VALUES (2, 'b')")["message"] == "INSERT 1"
    assert a.execute("SELECT COUNT(*) FROM t")["rows"] == [[2]]
    a.close()
    b.close()


def test_disconnect_rolls_back_open_transaction(server):
    a = connect("127.0.0.1", server)
    a.execute("CREATE TABLE t (id INT PRIMARY KEY, v TEXT)")
    a.execute("INSERT INTO t VALUES (1, 'committed')")
    a.execute("BEGIN")
    a.execute("INSERT INTO t VALUES (2, 'pending')")
    a.close()  # drop the connection mid-transaction -> server rolls it back

    time.sleep(0.05)
    b = connect("127.0.0.1", server)
    assert b.execute("SELECT COUNT(*) FROM t")["rows"] == [[1]]
    b.close()


def test_server_reports_errors(server):
    c = connect("127.0.0.1", server)
    with pytest.raises(ServerError):
        c.execute("SELECT * FROM nonexistent")
    c.close()
