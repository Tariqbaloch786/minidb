import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from minidb import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def dbfile(tmp_path):
    """A persistent database backed by a real file on disk."""
    path = str(tmp_path / "test.db")
    database = Database(path)
    yield database, path
    database.close()
