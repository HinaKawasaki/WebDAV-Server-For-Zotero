# tests/test_users.py
import contextlib
import hashlib
import unittest
from unittest import mock

from server import db, passwords, users

SETTINGS = {"table": "dav_users"}


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(sql.split()).lower()
        self.rowcount = 0
        self._one = None
        self._all = []
        if norm.startswith("select `password`"):
            value = self.store.get(params[0])
            if value is not None:
                self._one = {"password": value}
                self.rowcount = 1
        elif norm.startswith("select count(*)"):
            self._one = {"n": len(self.store)}
        elif norm.startswith("select `username`"):
            self._all = [{"username": name} for name in sorted(self.store)]
        elif norm.startswith("update"):
            digest, name = params
            if name in self.store:
                self.store[name] = digest
                self.rowcount = 1
        elif norm.startswith("insert"):
            name, digest = params
            if name in self.store:
                raise db.DatabaseError("(1062, \"Duplicate entry 'x'\")")
            self.store[name] = digest
            self.rowcount = 1
        elif norm.startswith("delete"):
            if self.store.pop(params[0], None) is not None:
                self.rowcount = 1
        elif norm.startswith("create table"):
            pass
        else:
            raise AssertionError(f"unexpected SQL: {norm}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConnection:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def close(self):
        pass


def fake_connect(store):
    @contextlib.contextmanager
    def _connect(_settings):
        yield FakeConnection(store)

    return _connect


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.patch = mock.patch.object(db, "connect", fake_connect(self.store))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_add_and_authenticate(self):
        users.add_user(SETTINGS, "alice", "s3cret")
        self.assertTrue(users.authenticate(SETTINGS, "alice", "s3cret"))
        self.assertFalse(users.authenticate(SETTINGS, "alice", "wrong"))
        self.assertFalse(users.authenticate(SETTINGS, "nobody", "s3cret"))
        self.assertFalse(users.authenticate(SETTINGS, "", "s3cret"))
        self.assertFalse(users.authenticate(SETTINGS, "alice", ""))

    def test_password_is_not_stored_in_clear(self):
        users.add_user(SETTINGS, "alice", "s3cret")
        self.assertNotIn("s3cret", self.store["alice"])
        self.assertTrue(self.store["alice"].startswith("scrypt$"))

    def test_duplicate_user(self):
        users.add_user(SETTINGS, "alice", "s3cret")
        with self.assertRaises(users.UserExistsError):
            users.add_user(SETTINGS, "alice", "other")

    def test_set_password_missing_user(self):
        with self.assertRaises(users.UserNotFoundError):
            users.set_password(SETTINGS, "ghost", "s3cret")

    def test_set_password(self):
        users.add_user(SETTINGS, "alice", "old")
        users.set_password(SETTINGS, "alice", "new")
        self.assertFalse(users.authenticate(SETTINGS, "alice", "old"))
        self.assertTrue(users.authenticate(SETTINGS, "alice", "new"))

    def test_count_list_delete(self):
        users.add_user(SETTINGS, "bob", "x")
        users.add_user(SETTINGS, "alice", "y")
        self.assertEqual(users.count_users(SETTINGS), 2)
        self.assertEqual(users.list_users(SETTINGS), ["alice", "bob"])
        self.assertTrue(users.delete_user(SETTINGS, "alice"))
        self.assertFalse(users.delete_user(SETTINGS, "alice"))
        self.assertEqual(users.list_users(SETTINGS), ["bob"])

    def test_username_validation(self):
        with self.assertRaises(ValueError):
            users.add_user(SETTINGS, "", "x")
        with self.assertRaises(ValueError):
            users.add_user(SETTINGS, "a" * 65, "x")
        with self.assertRaises(ValueError):
            users.add_user(SETTINGS, "bad\x00name", "x")

    def test_table_name_is_validated(self):
        with self.assertRaises(db.DatabaseError):
            users.count_users({"table": "dav_users; DROP TABLE x"})


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.patch = mock.patch.object(db, "connect", fake_connect(self.store))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_plaintext_is_upgraded_on_login(self):
        self.store["admin"] = "admin"
        self.assertTrue(users.authenticate(SETTINGS, "admin", "admin"))
        self.assertTrue(self.store["admin"].startswith("scrypt$"))
        self.assertFalse(users.authenticate(SETTINGS, "admin", "wrong"))

    def test_legacy_sha256_is_upgraded_on_login(self):
        self.store["admin"] = hashlib.sha256(b"admin").hexdigest()
        self.assertTrue(users.authenticate(SETTINGS, "admin", "admin"))
        self.assertTrue(self.store["admin"].startswith("scrypt$"))

    def test_failed_login_does_not_touch_the_stored_value(self):
        self.store["admin"] = "admin"
        self.assertFalse(users.authenticate(SETTINGS, "admin", "nope"))
        self.assertEqual(self.store["admin"], "admin")

    def test_scrypt_value_is_not_rewritten(self):
        stored = passwords.hash_password("admin")
        self.store["admin"] = stored
        self.assertTrue(users.authenticate(SETTINGS, "admin", "admin"))
        self.assertEqual(self.store["admin"], stored)


class EnsureSchemaTests(unittest.TestCase):
    """A deployment may lack the CREATE privilege on purpose."""

    @staticmethod
    def _connect(_settings):
        @contextlib.contextmanager
        def _ctx():
            yield FakeConnection({})

        return _ctx()

    def test_missing_create_privilege_is_tolerated_when_table_is_usable(self):
        with mock.patch.object(db, "connect", self._connect), mock.patch.object(
            db,
            "execute",
            side_effect=db.DatabaseError("(1142, 'CREATE command denied')", code=1142),
        ), mock.patch.object(db, "fetch_one", return_value={"1": 1}):
            users.ensure_schema(SETTINGS)  # must not raise

    def test_missing_table_without_create_privilege_raises(self):
        with mock.patch.object(db, "connect", self._connect), mock.patch.object(
            db,
            "execute",
            side_effect=db.DatabaseError("(1142, 'CREATE command denied')", code=1142),
        ), mock.patch.object(
            db,
            "fetch_one",
            side_effect=db.DatabaseError("(1146, \"Table ... doesn't exist\")", code=1146),
        ):
            with self.assertRaises(db.DatabaseError) as ctx:
                users.ensure_schema(SETTINGS)
            self.assertIn("does not exist", str(ctx.exception))

    def test_unrelated_errors_are_not_swallowed(self):
        with mock.patch.object(db, "connect", self._connect), mock.patch.object(
            db,
            "execute",
            side_effect=db.DatabaseError("(2013, 'Lost connection')", code=2013),
        ):
            with self.assertRaises(db.DatabaseError):
                users.ensure_schema(SETTINGS)


class SchemaCheckTests(unittest.TestCase):
    @staticmethod
    def _connect(_settings):
        @contextlib.contextmanager
        def _ctx():
            yield FakeConnection({})

        return _ctx()

    def _check(self, rows):
        with mock.patch.object(db, "connect", self._connect), mock.patch.object(
            db, "fetch_all", return_value=rows
        ):
            return users.check_schema(SETTINGS)

    def test_narrow_password_column_raises_with_the_fix(self):
        with self.assertRaises(db.DatabaseError) as ctx:
            self._check(
                [{"COLUMN_NAME": "password", "CHARACTER_MAXIMUM_LENGTH": 40}]
            )
        message = str(ctx.exception)
        self.assertIn("ALTER TABLE", message)
        self.assertIn("VARCHAR", message)

    def test_roomy_column_has_no_warning(self):
        info = self._check(
            [{"COLUMN_NAME": "password", "CHARACTER_MAXIMUM_LENGTH": 255}]
        )
        self.assertEqual(info["password_column"], 255)
        self.assertEqual(info["warnings"], [])

    def test_varchar64_is_accepted_with_a_warning(self):
        info = self._check(
            [{"COLUMN_NAME": "password", "CHARACTER_MAXIMUM_LENGTH": 64}]
        )
        self.assertEqual(len(info["warnings"]), 1)

    def test_introspection_failure_is_not_fatal(self):
        with mock.patch.object(db, "connect", self._connect), mock.patch.object(
            db,
            "fetch_all",
            side_effect=db.DatabaseError("(1142, denied)", code=1142),
        ):
            info = users.check_schema(SETTINGS)
        self.assertIsNone(info["password_column"])


if __name__ == "__main__":
    unittest.main()
