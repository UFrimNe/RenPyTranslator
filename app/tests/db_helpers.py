"""Commit/rollback test transactions and close SQLite handles deterministically on Windows."""
from contextlib import closing, contextmanager
import sqlite3


@contextmanager
def connection(*args, **kwargs):
    with closing(sqlite3.connect(*args, **kwargs)) as db:
        with db:
            yield db
