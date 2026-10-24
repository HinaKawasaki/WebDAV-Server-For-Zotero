# tests/test_davstate.py
"""Dead properties and locks must survive a restart.

The stores are injected, so these tests run against in-memory fakes - but the
managers themselves are WsgiDAV's real code plus our real mapping logic.
"""
import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import conf, davstate, db, passwords
from tests.test_mw import make_environ, wsgi_request
from tests.test_users import fake_connect


class FakePropertyStore:
    """Stand-in for :class:`davstate.MariaDBPropertyStore`."""

    def __init__(self):
        self.rows = {}
        self.batch_calls = []
        self.get_calls = 0

    def get(self, ref_url):
        self.get_calls += 1
        return dict(self.rows.get(ref_url, {}))

    def get_under(self, prefix, depth="infinity"):
        self.batch_calls.append((prefix, depth))
        result = {}
        for url, props in self.rows.items():
            if not url.startswith(prefix):
                continue
            if depth == "1":
                rest = url[len(prefix):]
                if rest and "/" in rest.rstrip("/"):
                    continue
            result[url] = dict(props)
        return result

    def urls(self):
        return sorted(self.rows)

    def count(self):
        return len(self.rows)

    def replace(self, ref_url, props):
        self.rows[ref_url] = dict(props)

    def drop(self, ref_url):
        self.rows.pop(ref_url, None)


class FakeLockStore:
    """Stand-in for :class:`davstate.MariaDBLockStore`."""

    def __init__(self):
        self.data = {}
        self.applied = []
        self.saves = 0

    def load(self):
        # mirrors MariaDBLockStore: one JSON round trip, bytes wrapped
        return davstate._decode_value(json.loads(json.dumps(self.data)))

    def save(self, locks):
        self.saves += 1
        self.data = json.loads(json.dumps(davstate._encode_value(locks)))

    def apply(self, upserts, deletes):
        self.applied.append((dict(upserts), list(deletes)))
        encoded = json.loads(json.dumps(davstate._encode_value(upserts)))
        for key in deletes:
            self.data.pop(key, None)
        for key, value in encoded.items():
            self.data[key] = value

    def clear(self):
        self.data = {}


class PropertyManagerTests(unittest.TestCase):
    def setUp(self):
        self.store = FakePropertyStore()
        self.manager = davstate.MariaDBPropertyManager(self.store)

    def test_write_then_read(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.assertEqual(self.manager.get_properties("/hello/a.txt"), ["{DAV:}tag"])
        self.assertEqual(self.manager.get_property("/hello/a.txt", "{DAV:}tag"), "<x>1</x>")

    def test_unknown_resource_has_no_properties(self):
        self.assertIsNone(self.manager.get_property("/hello/none.txt", "{DAV:}tag"))
        self.assertEqual(self.manager.get_properties("/hello/none.txt"), [])

    def test_properties_survive_a_restart(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        # a new manager over the same database == a restarted service
        restarted = davstate.MariaDBPropertyManager(self.store)
        self.assertEqual(restarted.get_property("/hello/a.txt", "{DAV:}tag"), "<x>1</x>")

    def test_overwrite_keeps_one_value(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>2</x>")
        self.assertEqual(self.manager.get_property("/hello/a.txt", "{DAV:}tag"), "<x>2</x>")
        self.assertEqual(len(self.store.get("/hello/a.txt")), 1)

    def test_properties_are_independent(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.write_property("/hello/b.txt", "{DAV:}tag", "<x>2</x>")
        self.assertEqual(self.manager.get_property("/hello/a.txt", "{DAV:}tag"), "<x>1</x>")
        self.assertEqual(self.manager.get_property("/hello/b.txt", "{DAV:}tag"), "<x>2</x>")

    def test_remove_property(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.remove_property("/hello/a.txt", "{DAV:}tag")
        self.assertIsNone(self.manager.get_property("/hello/a.txt", "{DAV:}tag"))

    def test_remove_unknown_property_is_not_an_error(self):
        self.manager.remove_property("/hello/a.txt", "{DAV:}nothere")

    def test_remove_properties(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.remove_properties("/hello/a.txt")
        self.assertEqual(self.manager.get_properties("/hello/a.txt"), [])

    def test_copy_properties(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.copy_properties("/hello/a.txt", "/hello/copy.txt")
        self.assertEqual(self.manager.get_property("/hello/copy.txt", "{DAV:}tag"), "<x>1</x>")
        self.assertEqual(self.manager.get_property("/hello/a.txt", "{DAV:}tag"), "<x>1</x>")

    def test_move_properties(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.move_properties("/hello/a.txt", "/hello/b.txt", False)
        self.assertIsNone(self.manager.get_property("/hello/a.txt", "{DAV:}tag"))
        self.assertEqual(self.manager.get_property("/hello/b.txt", "{DAV:}tag"), "<x>1</x>")

    def test_move_properties_with_children(self):
        self.manager.write_property("/hello/dir/", "{DAV:}tag", "<dir>")
        self.manager.write_property("/hello/dir/a.txt", "{DAV:}tag", "<file>")
        self.manager.move_properties("/hello/dir/", "/hello/new/", True)
        self.assertIsNone(self.manager.get_property("/hello/dir/a.txt", "{DAV:}tag"))
        self.assertEqual(self.manager.get_property("/hello/new/", "{DAV:}tag"), "<dir>")
        self.assertEqual(self.manager.get_property("/hello/new/a.txt", "{DAV:}tag"), "<file>")

    def test_deleting_a_collection_removes_only_its_own_properties(self):
        # WsgiDAV deletes children one by one, so a collection delete must not
        # cascade - otherwise MOVE with children breaks.
        self.manager.write_property("/hello/dir/", "{DAV:}tag", "<dir>")
        self.manager.write_property("/hello/dir/a.txt", "{DAV:}tag", "<file>")
        self.manager.remove_properties("/hello/dir/")
        self.assertIsNone(self.manager.get_property("/hello/dir/", "{DAV:}tag"))
        self.assertEqual(self.manager.get_property("/hello/dir/a.txt", "{DAV:}tag"), "<file>")

    def test_stored_urls_are_enumerated(self):
        self.manager.write_property("/hello/a.txt", "{DAV:}tag", "<x>1</x>")
        self.manager.write_property("/hello/b.txt", "{DAV:}tag", "<x>1</x>")
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.urls(), ["/hello/a.txt", "/hello/b.txt"])


def make_lock(root, timeout=3600):
    """A lock *definition*: create() adds the token and the expire date."""
    return {
        "root": root,
        "principal": "hello",
        "depth": "0",
        "scope": "exclusive",
        "type": "write",
        "owner": b"<owner/>",  # WsgiDAV requires an XML bytestring
        "timeout": timeout,
    }


class LockStorageTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeLockStore()
        self.storage = davstate.MariaDBLockStorage(self.store)
        self.storage.open()
        self.addCleanup(self.storage.close)

    def _acquire(self, path="/hello/a.txt", timeout=3600):
        lock = self.storage.create(path, make_lock(path, timeout))
        return lock["token"]

    def test_create_then_get(self):
        token = self._acquire()
        lock = self.storage.get(token)
        self.assertIsNotNone(lock)
        self.assertEqual(lock["root"], "/hello/a.txt")
        self.assertTrue(token.startswith("opaquelocktoken:"))

    def test_locks_survive_a_restart(self):
        token = self._acquire()
        revived = davstate.MariaDBLockStorage(self.store)
        revived.open()
        try:
            lock = revived.get(token)
            self.assertIsNotNone(lock)
            self.assertEqual(lock["principal"], "hello")
        finally:
            revived.close()

    def test_delete_removes_the_lock(self):
        token = self._acquire()
        self.storage.delete(token)
        self.assertIsNone(self.storage.get(token))
        revived = davstate.MariaDBLockStorage(self.store)
        revived.open()
        try:
            self.assertIsNone(revived.get(token))
            self.assertEqual(revived._dict, {})
        finally:
            revived.close()

    def test_expired_locks_are_not_revived(self):
        token = self._acquire()
        # age the stored lock instead of sleeping
        self.store.data[token]["expire"] = time.time() - 5
        revived = davstate.MariaDBLockStorage(self.store)
        revived.open()
        try:
            self.assertIsNone(revived.get(token))
        finally:
            revived.close()
        self.assertEqual(self.store.data, {})

    def test_clear_empties_the_table(self):
        self._acquire()
        self.storage.clear()
        self.assertEqual(self.store.data, {})

    def test_create_writes_only_the_new_rows(self):
        token = self._acquire()
        self.assertEqual(len(self.store.applied), 1)
        upserts, deletes = self.store.applied[-1]
        self.assertEqual(deletes, [])
        self.assertEqual(set(upserts), {token, "URL2TOKEN:/hello/a.txt"})

    def test_delete_removes_only_the_touched_rows(self):
        token = self._acquire()
        other = self._acquire("/hello/b.txt")
        self.store.applied.clear()
        self.storage.delete(token)
        upserts, deletes = self.store.applied[-1]
        self.assertEqual(upserts, {})
        self.assertCountEqual(deletes, [token, "URL2TOKEN:/hello/a.txt"])
        self.assertEqual(set(self.store.data), {other, "URL2TOKEN:/hello/b.txt"})

    def test_noop_flush_does_not_touch_the_store(self):
        self._acquire()
        self.store.applied.clear()
        self.storage._flush()
        self.assertEqual(self.store.applied, [])

    def test_cleanup_persists_dangling_reference_removal(self):
        token = self._acquire()
        self.store.data["URL2TOKEN:/hello/stale.txt"] = ["opaquelocktoken:missing"]
        revived = davstate.MariaDBLockStorage(self.store)
        revived.open()
        try:
            self.assertNotIn("URL2TOKEN:/hello/stale.txt", self.store.data)
            self.assertIn(token, self.store.data)
        finally:
            revived.close()

    def test_expired_lock_is_deleted_without_rewriting_the_table(self):
        token = self._acquire()
        other = self._acquire("/hello/b.txt")
        self.storage._dict[token]["expire"] = time.time() - 60
        self.store.applied.clear()
        self.storage.cleanup()
        upserts, deletes = self.store.applied[-1]
        self.assertEqual(upserts, {})
        self.assertCountEqual(deletes, [token, "URL2TOKEN:/hello/a.txt"])
        self.assertEqual(set(self.store.data), {other, "URL2TOKEN:/hello/b.txt"})

    def test_lock_list(self):
        token = self._acquire()
        tokens = self.storage.get_lock_list(
            "/hello/a.txt", include_root=True, include_children=False, token_only=True
        )
        self.assertEqual(tokens, [token])


class FakeConnection:
    def __init__(self):
        self.began = 0
        self.commits = 0
        self.rollbacks = 0

    def begin(self):
        self.began += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class SqlLayerTests(unittest.TestCase):
    """Check the statements we send, without a database."""

    def setUp(self):
        self.executed = []
        self.conn = FakeConnection()

        @contextlib.contextmanager
        def fake_connect(settings):
            yield self.conn

        def fake_execute(conn, sql, params=None):
            self.executed.append(("execute", " ".join(sql.split()), params))
            return 1

        def fake_fetch_all(conn, sql, params=None):
            self.executed.append(("fetch_all", " ".join(sql.split()), params))
            return []

        self.patches = [
            mock.patch.object(davstate.db, "connect", fake_connect),
            mock.patch.object(davstate.db, "execute", fake_execute),
            mock.patch.object(davstate.db, "fetch_all", fake_fetch_all),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_replace_deletes_then_inserts_in_one_transaction(self):
        store = davstate.MariaDBPropertyStore({}, "dav_properties")
        store.replace("/hello/a.txt", {"{DAV:}tag": "<x>1</x>"})
        kinds = [entry[0] for entry in self.executed]
        self.assertEqual(kinds, ["execute", "execute"])
        self.assertIn("DELETE FROM `dav_properties`", self.executed[0][1])
        self.assertIn("INSERT INTO `dav_properties`", self.executed[1][1])
        self.assertEqual(self.conn.began, 1)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 0)
        # the URL is keyed by a hash, and the value is stored verbatim
        self.assertEqual(self.executed[0][2][0], davstate._digest("/hello/a.txt"))
        self.assertEqual(self.executed[1][2][1], "{DAV:}tag")
        self.assertEqual(self.executed[1][2][2], "<x>1</x>")

    def test_drop_targets_a_single_url(self):
        store = davstate.MariaDBPropertyStore({}, "dav_properties")
        store.drop("/hello/dir/a.txt")
        self.assertEqual(len(self.executed), 1)
        self.assertIn("DELETE FROM `dav_properties`", self.executed[0][1])
        self.assertEqual(self.executed[0][2][0], davstate._digest("/hello/dir/a.txt"))

    def test_lock_save_is_a_single_transaction(self):
        store = davstate.MariaDBLockStore({}, "dav_locks")
        store.save({"opaquelocktoken:x": {"root": "/hello/a.txt", "owner": b"<o/>"}})
        self.assertEqual(len(self.executed), 2)
        self.assertIn("DELETE FROM `dav_locks`", self.executed[0][1])
        self.assertIn("INSERT INTO `dav_locks`", self.executed[1][1])
        stored = json.loads(self.executed[1][2][1])
        self.assertEqual(stored["root"], "/hello/a.txt")
        # bytes survive the JSON round trip through an explicit marker
        self.assertEqual(stored["owner"], {"__dav_bytes__": "PG8vPg=="})
        self.assertEqual(davstate._decode_value(stored)["owner"], b"<o/>")
        self.assertEqual(self.conn.commits, 1)

    def test_lock_apply_only_touches_changed_rows(self):
        store = davstate.MariaDBLockStore({}, "dav_locks")
        store.apply(
            {"opaquelocktoken:x": {"root": "/hello/a.txt"}}, ["opaquelocktoken:y"]
        )
        self.assertEqual([entry[0] for entry in self.executed], ["execute", "execute"])
        self.assertIn(
            "DELETE FROM `dav_locks` WHERE `lock_key` = %s", self.executed[0][1]
        )
        self.assertEqual(self.executed[0][2], ("opaquelocktoken:y",))
        self.assertIn("ON DUPLICATE KEY UPDATE", self.executed[1][1])
        self.assertEqual(self.executed[1][2][0], "opaquelocktoken:x")
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 0)

    def test_lock_apply_without_changes_is_a_noop(self):
        davstate.MariaDBLockStore({}, "dav_locks").apply({}, [])
        self.assertEqual(self.executed, [])

    def test_lock_load_skips_unreadable_rows(self):
        self.patches[2].stop()
        rows = [{"lock_key": "good", "lock_value": '{"root": "/x"}'},
                {"lock_key": "bad", "lock_value": "not json"}]
        with mock.patch.object(davstate.db, "fetch_all", lambda *a, **k: rows):
            loaded = davstate.MariaDBLockStore({}, "dav_locks").load()
        self.assertEqual(loaded, {"good": {"root": "/x"}})

    def test_get_under_depth_1_is_one_prefix_query(self):
        self.patches[2].stop()
        rows = [
            {"ref_url": "/alice/", "prop_name": "{DAV:}tag", "prop_value": "root"},
            {"ref_url": "/alice/a.txt", "prop_name": "{DAV:}tag", "prop_value": "a"},
            {"ref_url": "/alice/sub/", "prop_name": "{DAV:}tag", "prop_value": "sub"},
        ]
        calls = []

        def fake_fetch_all(conn, sql, params=None):
            calls.append((" ".join(sql.split()), params))
            return rows

        with mock.patch.object(davstate.db, "fetch_all", fake_fetch_all):
            store = davstate.MariaDBPropertyStore({}, "dav_properties")
            grouped = store.get_under("/alice/", depth="1")

        self.assertEqual(
            grouped,
            {
                "/alice/": {"{DAV:}tag": "root"},
                "/alice/a.txt": {"{DAV:}tag": "a"},
                "/alice/sub/": {"{DAV:}tag": "sub"},
            },
        )
        self.assertEqual(len(calls), 1)
        sql, params = calls[0]
        self.assertIn("`ref_url` LIKE %s ESCAPE '!'", sql)
        self.assertIn("LOCATE('/', SUBSTRING(`ref_url`, %s))", sql)
        self.assertEqual(params, ["/alice/%", 8, 8, 8])

    def test_get_under_infinity_has_no_depth_filter(self):
        self.patches[2].stop()
        calls = []

        def fake_fetch_all(conn, sql, params=None):
            calls.append((" ".join(sql.split()), params))
            return []

        with mock.patch.object(davstate.db, "fetch_all", fake_fetch_all):
            davstate.MariaDBPropertyStore({}, "dav_properties").get_under("/alice/")

        self.assertEqual(calls[0][1], ["/alice/%"])
        self.assertNotIn("LOCATE", calls[0][0])

    def test_like_prefix_escapes_wildcards(self):
        self.assertEqual(davstate._like_prefix("/a%b_c!d/"), "/a!%b!_c!!d/")


class RequestCacheTests(unittest.TestCase):
    """A PROPFIND over a collection must read the property table once."""

    def setUp(self):
        self.store = FakePropertyStore()
        self.store.rows = {
            "/alice/": {"{DAV:}root": "r"},
            "/alice/a.txt": {"{DAV:}tag": "a"},
            "/alice/sub/": {"{DAV:}tag": "s"},
            "/alice/sub/b.txt": {"{DAV:}tag": "b"},
        }
        self.manager = davstate.MariaDBPropertyManager(self.store)

    def test_depth_1_batches_the_collection_and_its_children(self):
        environ = {"REQUEST_METHOD": "PROPFIND", "HTTP_DEPTH": "1"}
        self.assertEqual(
            self.manager.get_properties("/alice/", environ), ["{DAV:}root"]
        )
        self.assertEqual(
            self.manager.get_properties("/alice/a.txt", environ), ["{DAV:}tag"]
        )
        self.assertEqual(
            self.manager.get_property("/alice/a.txt", "{DAV:}tag", environ), "a"
        )
        self.assertEqual(
            self.manager.get_property("/alice/sub/", "{DAV:}tag", environ), "s"
        )
        self.assertEqual(self.store.batch_calls, [("/alice/", "1")])
        self.assertEqual(self.store.get_calls, 0)

    def test_grandchild_falls_back_to_a_single_query(self):
        environ = {"REQUEST_METHOD": "PROPFIND", "HTTP_DEPTH": "1"}
        self.manager.get_properties("/alice/", environ)
        self.manager.get_properties("/alice/sub/b.txt", environ)
        # PropertyManager itself touches the mapping twice per lookup
        self.assertGreaterEqual(self.store.get_calls, 1)

    def test_infinity_covers_the_whole_subtree(self):
        environ = {"REQUEST_METHOD": "PROPFIND", "HTTP_DEPTH": "infinity"}
        self.manager.get_properties("/alice/", environ)
        self.assertEqual(
            self.manager.get_properties("/alice/sub/b.txt", environ), ["{DAV:}tag"]
        )
        self.assertEqual(self.store.get_calls, 0)

    def test_no_batch_for_depth_0_or_other_methods(self):
        for environ in (
            {"REQUEST_METHOD": "PROPFIND", "HTTP_DEPTH": "0"},
            {"REQUEST_METHOD": "GET"},
        ):
            self.manager.get_properties("/alice/", environ)
        self.assertEqual(self.store.batch_calls, [])
        self.assertGreaterEqual(self.store.get_calls, 2)

    def test_missing_property_is_authoritative_from_the_batch(self):
        self.store.rows.pop("/alice/a.txt")
        environ = {"REQUEST_METHOD": "PROPFIND", "HTTP_DEPTH": "1"}
        self.manager.get_properties("/alice/", environ)
        self.assertEqual(self.manager.get_properties("/alice/a.txt", environ), [])
        self.assertEqual(self.store.get_calls, 0)


class EnsureSchemaTests(unittest.TestCase):
    def _run(self, execute, table_exists=True, tables=("dav_properties", "dav_locks")):
        statements = []

        def record(conn, sql, params=None):
            statements.append(" ".join(sql.split()))
            return execute(conn, sql, params)

        @contextlib.contextmanager
        def fake_connect(settings):
            yield FakeConnection()

        with (
            mock.patch.object(davstate.db, "connect", fake_connect),
            mock.patch.object(davstate.db, "execute", record),
            mock.patch.object(davstate, "_table_exists", lambda settings, table: table_exists),
        ):
            result = davstate.ensure_schema({}, *tables)
        return result, statements

    def test_creates_both_tables_when_allowed(self):
        result, statements = self._run(lambda *a, **k: 0)
        self.assertIn("CREATE TABLE IF NOT EXISTS `dav_properties`", statements[0])
        self.assertIn("PRIMARY KEY (`ref_hash`, `name_hash`)", statements[0])
        self.assertIn("CREATE TABLE IF NOT EXISTS `dav_locks`", statements[1])
        self.assertIn("PRIMARY KEY (`lock_key`)", statements[1])
        self.assertEqual(result["warnings"], [])

    def test_existing_tables_without_create_privilege_are_used(self):
        def deny(conn, sql, params=None):
            raise davstate.DatabaseError("CREATE command denied", code=1142)

        result, _ = self._run(deny, table_exists=True)
        self.assertTrue(result["properties_ready"])
        self.assertTrue(result["locks_ready"])
        self.assertEqual(result["warnings"], [])

    def test_missing_table_without_create_privilege_degrades_gracefully(self):
        # a missing optional table must not stop the service from starting
        def deny(conn, sql, params=None):
            raise davstate.DatabaseError("CREATE command denied", code=1142)

        result, _ = self._run(deny, table_exists=False)
        self.assertFalse(result["properties_ready"])
        self.assertFalse(result["locks_ready"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_unexpected_error_degrades_when_the_table_is_missing(self):
        # e.g. an old row format rejecting the lock key: optional feature, so
        # the service must still start (in-memory fallback + warning)
        def boom(conn, sql, params=None):
            raise davstate.DatabaseError("Specified key was too long", code=1071)

        result, _ = self._run(boom, table_exists=False)
        self.assertFalse(result["properties_ready"])
        self.assertFalse(result["locks_ready"])
        self.assertEqual(len(result["warnings"]), 2)

    def test_unexpected_error_keeps_working_with_an_existing_table(self):
        def boom(conn, sql, params=None):
            raise davstate.DatabaseError("syntax error", code=1064)

        result, _ = self._run(boom, table_exists=True)
        self.assertTrue(result["properties_ready"])
        self.assertTrue(result["locks_ready"])
        # worth a warning: our CREATE did not run, so the table's shape is
        # unverified - but it must not stop the service
        self.assertEqual(len(result["warnings"]), 2)


PROPPATCH_BODY = (
    b'<?xml version="1.0" encoding="utf-8" ?>'
    b'<D:propertyupdate xmlns:D="DAV:" xmlns:X="http://example.com/ns">'
    b"<D:set><D:prop><X:tag>kept</X:tag></D:prop></D:set></D:propertyupdate>"
)
PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8" ?>'
    b'<D:propfind xmlns:D="DAV:" xmlns:X="http://example.com/ns">'
    b"<D:prop><X:tag/></D:prop></D:propfind>"
)


class EndToEndPersistenceTests(unittest.TestCase):
    """The whole WSGI stack, with the stores replaced by shared fakes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        share = Path(self._tmp.name) / "share"
        share.mkdir()
        self.property_store = FakePropertyStore()
        self.user_store = {"alice": passwords.hash_password("pw")}
        for patcher in (
            mock.patch.object(conf, "SHARE_DIR", share),
            mock.patch.object(db, "connect", fake_connect(self.user_store)),
            # the fake database only speaks the user table
            mock.patch.object(davstate, "_table_usable", lambda *a, **kw: True),
            # same "database" for both app instances built below
            mock.patch.object(
                davstate, "MariaDBPropertyStore", lambda *a, **kw: self.property_store
            ),
            mock.patch.object(
                davstate, "MariaDBLockStore", lambda *a, **kw: FakeLockStore()
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _app(self):
        from server.app import create_dav_app

        return create_dav_app(conf.build_config())

    def test_dead_property_survives_a_restart(self):
        app = self._app()
        result = wsgi_request(
            app,
            make_environ("PUT", "/alice/note.txt", credentials=("alice", "pw"), body=b"hi"),
        )
        self.assertEqual(result["code"], 201)

        result = wsgi_request(
            app,
            make_environ(
                "PROPPATCH", "/alice/note.txt", credentials=("alice", "pw"),
                body=PROPPATCH_BODY,
            ),
        )
        self.assertEqual(result["code"], 207, result["body"][:200])

        # a new app over the same store == a restarted service
        restarted = self._app()
        result = wsgi_request(
            restarted,
            make_environ(
                "PROPFIND", "/alice/note.txt", credentials=("alice", "pw"),
                body=PROPFIND_BODY,
            ),
        )
        self.assertEqual(result["code"], 207)
        self.assertIn(b"kept", result["body"])

    def test_propfind_depth_1_reads_the_property_table_once(self):
        app = self._app()
        creds = ("alice", "pw")
        for name in ("a", "b", "c"):
            result = wsgi_request(
                app, make_environ("PUT", f"/alice/{name}.txt", credentials=creds, body=b"x")
            )
            self.assertEqual(result["code"], 201)
            result = wsgi_request(
                app,
                make_environ(
                    "PROPPATCH", f"/alice/{name}.txt", credentials=creds,
                    body=PROPPATCH_BODY,
                ),
            )
            self.assertEqual(result["code"], 207)

        environ = make_environ("PROPFIND", "/alice/", credentials=creds, body=PROPFIND_BODY)
        environ["HTTP_DEPTH"] = "1"
        # PROPPATCH above also touches the store; measure only the PROPFIND
        self.property_store.get_calls = 0
        self.property_store.batch_calls.clear()
        result = wsgi_request(app, environ)
        self.assertEqual(result["code"], 207)
        self.assertEqual(result["body"].count(b"kept"), 3)
        # the whole PROPFIND was served from one batch query
        self.assertEqual(self.property_store.batch_calls, [("/alice/", "1")])
        self.assertEqual(self.property_store.get_calls, 0)

    def test_property_disappears_when_the_resource_is_deleted(self):
        app = self._app()
        wsgi_request(
            app,
            make_environ("PUT", "/alice/gone.txt", credentials=("alice", "pw"), body=b"hi"),
        )
        wsgi_request(
            app,
            make_environ(
                "PROPPATCH", "/alice/gone.txt", credentials=("alice", "pw"),
                body=PROPPATCH_BODY,
            ),
        )
        self.assertTrue(self.property_store.rows)
        result = wsgi_request(
            app,
            make_environ("DELETE", "/alice/gone.txt", credentials=("alice", "pw")),
        )
        self.assertEqual(result["code"], 204)
        self.assertEqual(self.property_store.rows, {})


class AttachTests(unittest.TestCase):
    def _config(self, **state):
        return {
            "webdav_db": {"user": "webdav_app", "database": "webdav"},
            "webdav_dav_state": {"persist_properties": True, "persist_locks": True, **state},
        }

    def _attach(self, config, usable=True):
        with mock.patch.object(davstate, "_table_usable", lambda *a, **kw: usable):
            davstate.attach(config)

    def test_attaches_both_managers(self):
        config = self._config()
        self._attach(config)
        self.assertIsInstance(config["property_manager"], davstate.MariaDBPropertyManager)
        self.assertIsInstance(config["lock_storage"], davstate.MariaDBLockStorage)

    def test_can_disable_each_side(self):
        config = self._config(persist_properties=False, persist_locks=False)
        self._attach(config)
        self.assertNotIn("property_manager", config)
        self.assertNotIn("lock_storage", config)

    def test_unusable_table_keeps_the_in_memory_default(self):
        config = self._config()
        self._attach(config, usable=False)
        self.assertNotIn("property_manager", config)
        self.assertNotIn("lock_storage", config)

    def test_config_without_credentials_is_left_alone(self):
        config = {"webdav_db": {}}
        davstate.attach(config)
        self.assertNotIn("property_manager", config)


if __name__ == "__main__":
    unittest.main()
