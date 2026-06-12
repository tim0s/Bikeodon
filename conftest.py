import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


import sqlite3
import pytest


@pytest.fixture()
def conn_tracker(monkeypatch):
    """
    Patch database._conn so every opened connection is tracked.
    After the test, asserts that every connection was closed.

    Usage:
        def test_no_leak(db_path, conn_tracker):
            my_function(db_path)
            # fixture automatically asserts all connections are closed
    """
    import database as db_mod

    records = []

    # sqlite3.Connection.close is read-only, so we subclass instead of patching
    class TrackingConnection(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.row_factory = sqlite3.Row
            self._record = {"closed": False}
            records.append(self._record)

        def close(self):
            self._record["closed"] = True
            super().close()

    def tracking_conn(path):
        return TrackingConnection(path)

    monkeypatch.setattr(db_mod, "_conn", tracking_conn)

    # activitypub imports _conn as _db_conn at module load time, so patch it there too
    import activitypub as ap_mod
    monkeypatch.setattr(ap_mod, "_db_conn", tracking_conn)

    yield records

    leaked = sum(1 for r in records if not r["closed"])
    assert leaked == 0, (
        f"{leaked} of {len(records)} DB connection(s) opened but never closed"
    )
