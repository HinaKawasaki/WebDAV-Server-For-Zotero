# tests/test_db.py
"""The per-thread connection pool in server/db.py (no real database)."""
import unittest
from unittest import mock

from server import db

SETTINGS = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "webdav",
    "password": "pw",
    "database": "webdav",
}


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.rollbacks = 0
        self.pings = 0
        self.ping_error = None

    def ping(self, reconnect=True):
        self.pings += 1
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class ConnectionPoolTests(unittest.TestCase):
    def setUp(self):
        db.close_pooled()
        self.created = []

        def factory(**_kwargs):
            conn = FakeConnection()
            self.created.append(conn)
            return conn

        patcher = mock.patch.object(db.pymysql, "connect", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(db.close_pooled)

    def test_same_settings_reuse_the_connection(self):
        with db.connect(SETTINGS) as first:
            pass
        with db.connect(SETTINGS) as second:
            pass
        self.assertIs(first, second)
        self.assertEqual(len(self.created), 1)

    def test_a_different_database_gets_its_own_connection(self):
        other = dict(SETTINGS, database="other")
        with db.connect(SETTINGS) as first:
            pass
        with db.connect(other) as second:
            pass
        self.assertIsNot(first, second)
        self.assertEqual(len(self.created), 2)

    def test_close_pooled_closes_the_connection(self):
        with db.connect(SETTINGS) as first:
            pass
        db.close_pooled()
        self.assertTrue(first.closed)
        with db.connect(SETTINGS) as second:
            pass
        self.assertIsNot(first, second)

    def test_reuse_false_opens_and_closes(self):
        with db.connect(SETTINGS, reuse=False) as conn:
            pass
        self.assertTrue(conn.closed)

    def test_unexpected_error_discards_the_connection(self):
        with self.assertRaises(RuntimeError), db.connect(SETTINGS):
            raise RuntimeError("boom")
        self.assertTrue(self.created[0].closed)
        with db.connect(SETTINGS):
            pass
        self.assertEqual(len(self.created), 2)

    def test_connection_level_database_error_discards(self):
        with self.assertRaises(db.DatabaseError), db.connect(SETTINGS):
            raise db.DatabaseError("server has gone away", code=2006)
        self.assertTrue(self.created[0].closed)

    def test_business_database_error_keeps_the_connection(self):
        with self.assertRaises(db.DatabaseError), db.connect(SETTINGS):
            raise db.DatabaseError("duplicate entry", code=1062)
        self.assertFalse(self.created[0].closed)
        with db.connect(SETTINGS) as conn:
            self.assertIs(conn, self.created[0])
        self.assertEqual(self.created[0].rollbacks, 1)

    def test_idle_connection_is_pinged(self):
        with db.connect(SETTINGS) as first:
            pass
        clock = mock.patch.object(db.time, "monotonic", return_value=1e9)
        with clock, db.connect(SETTINGS) as second:
            self.assertIs(second, first)
        self.assertEqual(first.pings, 1)

    def test_stale_connection_is_replaced_when_ping_fails(self):
        with db.connect(SETTINGS) as first:
            pass
        first.ping_error = db.pymysql.err.OperationalError(2006, "gone away")
        clock = mock.patch.object(db.time, "monotonic", return_value=1e9)
        with clock, db.connect(SETTINGS) as second:
            pass
        self.assertIsNot(first, second)
        self.assertTrue(first.closed)
        self.assertEqual(len(self.created), 2)

    def test_pool_size_is_bounded_per_thread(self):
        for index in range(db._POOL_MAX_PER_THREAD + 2):
            settings = dict(SETTINGS, database=f"db{index}")
            with db.connect(settings):
                pass
        conns = db._pool_conns()
        self.assertLessEqual(len(conns), db._POOL_MAX_PER_THREAD)
        closed = [conn for conn in self.created if conn.closed]
        self.assertEqual(len(closed), 2)


if __name__ == "__main__":
    unittest.main()
