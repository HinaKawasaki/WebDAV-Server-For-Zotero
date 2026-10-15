# server/davstate.py
"""MariaDB-backed storage for DAV dead properties and locks.

WsgiDAV already separates *policy* from *storage*: ``LockManager`` takes a lock
storage object and ``property_manager`` takes a property manager instance.  Both
stock implementations keep everything in memory (or in a shelve file), so a
restart loses every PROPPATCHed property and every lock - and this service is
restarted on every deploy.

This module keeps WsgiDAV's policy code and swaps only the storage:

* :class:`MariaDBPropertyManager` subclasses ``PropertyManager`` and hands it a
  mapping that reads and writes the property table.  All of the copy/move/
  remove logic therefore stays upstream.
* :class:`MariaDBLockStorage` subclasses ``LockStorageDict`` and rewrites the
  lock table on every mutation, exactly like the stock ``LockStorageShelve``
  does with a shelve file.  Expired locks are pruned when the storage opens.

Both stores are injected, so the SQL can be replaced by an in-memory fake in
tests (see ``tests/test_davstate.py``).

SQL access needs only ``SELECT``/``INSERT``/``UPDATE``/``DELETE`` on the two
tables; the ``CREATE TABLE`` statements in :func:`ensure_schema` are issued once
and a missing ``CREATE`` privilege is tolerated when the tables already exist.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import MutableMapping

# wsgidav.util must be imported before wsgidav.dav_error pulls it in itself.
from wsgidav import util  # noqa: F401
from wsgidav.lock_man.lock_storage import LockStorageDict
from wsgidav.prop_man.property_manager import PropertyManager

from server import db
from server.db import DatabaseError

logger = logging.getLogger(__name__)

__all__ = [
    "MariaDBLockStorage",
    "MariaDBLockStore",
    "MariaDBPropertyManager",
    "MariaDBPropertyStore",
    "attach",
    "ensure_schema",
]

#: MySQL errors that mean "this account may not do that".
_PERMISSION_ERROR_CODES = {1044, 1142}

#: URL paths are hashed for the primary key, because a utf8mb4 index over a
#: 512 character column would exceed InnoDB's key length limit.
_HASH_BYTES = 32

_PROPERTIES_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    `ref_url`    VARCHAR(512) NOT NULL,
    `prop_name`  VARCHAR(255) NOT NULL,
    `prop_value` LONGTEXT     NOT NULL,
    `ref_hash`   BINARY(32)   NOT NULL,
    `name_hash`  BINARY(32)   NOT NULL,
    `updated_at` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`ref_hash`, `name_hash`),
    KEY `idx_{table}_url` (`ref_url`(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""

_LOCKS_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
    `lock_key`   VARCHAR(512) NOT NULL,
    `lock_value` LONGTEXT     NOT NULL,
    `updated_at` TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`lock_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
"""


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


#: WsgiDAV stores ``owner`` (and possibly other fields) as bytes, which JSON
#: cannot represent.  Wrap them in an explicit marker instead of pickling, so
#: the rows stay readable and no untrusted pickle is ever loaded.
_BYTES_KEY = "__dav_bytes__"


def _encode_value(value):
    if isinstance(value, bytes):
        return {_BYTES_KEY: base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]
    return value


def _decode_value(value):
    if isinstance(value, dict):
        if len(value) == 1 and _BYTES_KEY in value:
            return base64.b64decode(value[_BYTES_KEY])
        return {key: _decode_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_value(item) for item in value]
    return value


def _table_exists(settings, table: str) -> bool:
    with db.connect(settings) as conn:
        row = db.fetch_one(
            conn,
            "SELECT 1 AS ok FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
    return row is not None


def _table_usable(settings, table: str, what: str) -> bool:
    """True when the table can be read; a missing table is never fatal."""
    db.validate_identifier(table, f"{what} table name")
    try:
        with db.connect(settings) as conn:
            db.fetch_one(conn, f"SELECT 1 AS ok FROM `{table}` LIMIT 1")
    except DatabaseError as exc:
        logger.warning(
            "%s cannot be persisted in %r (%s) - falling back to memory",
            what,
            table,
            exc,
        )
        return False
    return True


def ensure_schema(settings, properties_table="dav_properties", locks_table="dav_locks") -> dict:
    """Create the two DAV state tables when allowed, and report what happened.

    Persisting dead properties and locks is a *feature*, not a prerequisite: a
    deployment whose account may not create tables (or which simply has not
    created them yet) keeps serving with WsgiDAV's in-memory managers, and this
    function reports that state so the caller can log it loudly.
    """
    result = {
        "properties_table": properties_table,
        "locks_table": locks_table,
        "properties_ready": False,
        "locks_ready": False,
        "warnings": [],
    }
    for key, label, table, ddl in (
        ("properties_ready", "properties", properties_table, _PROPERTIES_DDL),
        ("locks_ready", "locks", locks_table, _LOCKS_DDL),
    ):
        db.validate_identifier(table, f"{label} table name")
        try:
            with db.connect(settings) as conn:
                db.execute(conn, ddl.format(table=table))
            result[key] = True
        except DatabaseError as exc:
            if exc.code not in _PERMISSION_ERROR_CODES:
                raise
            if _table_exists(settings, table):
                logger.info("using the existing %s table %r", label, table)
                result[key] = True
            else:
                logger.warning(
                    "the %s table %r does not exist and this account may not create it; "
                    "%s stay in memory and are lost on restart",
                    label,
                    table,
                    label,
                )
                result["warnings"].append(
                    f"{label} table {table!r} is missing - {label} stay in memory; "
                    f"ask a DBA to create it to make them persistent"
                )
    return result


# --------------------------------------------------------------------------- #
# dead properties
# --------------------------------------------------------------------------- #
class MariaDBPropertyStore:
    """Property rows in MariaDB, keyed by the (normalised) URL WsgiDAV uses."""

    def __init__(self, settings, table="dav_properties"):
        self.settings = settings
        self.table = db.validate_identifier(table, "properties table")

    def __repr__(self):
        return f"MariaDBPropertyStore({self.table!r})"

    def get(self, ref_url: str) -> dict:
        with db.connect(self.settings) as conn:
            rows = db.fetch_all(
                conn,
                f"SELECT `prop_name`, `prop_value` FROM `{self.table}` WHERE `ref_hash` = %s",
                (_digest(ref_url),),
            )
        return {row["prop_name"]: row["prop_value"] for row in rows}

    def urls(self) -> list:
        with db.connect(self.settings) as conn:
            rows = db.fetch_all(
                conn, f"SELECT DISTINCT `ref_url` FROM `{self.table}` ORDER BY `ref_url`"
            )
        return [row["ref_url"] for row in rows]

    def count(self) -> int:
        with db.connect(self.settings) as conn:
            row = db.fetch_one(conn, f"SELECT COUNT(*) AS n FROM `{self.table}`")
        return int(row["n"]) if row else 0

    def replace(self, ref_url: str, props: dict) -> None:
        ref_hash = _digest(ref_url)
        with db.connect(self.settings) as conn:
            conn.begin()
            try:
                db.execute(conn, f"DELETE FROM `{self.table}` WHERE `ref_hash` = %s", (ref_hash,))
                for name, value in props.items():
                    db.execute(
                        conn,
                        f"INSERT INTO `{self.table}` "
                        "(`ref_url`, `prop_name`, `prop_value`, `ref_hash`, `name_hash`) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (ref_url, name, value, ref_hash, _digest(name)),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def drop(self, ref_url: str) -> None:
        """Remove the properties stored for exactly this URL.

        Deliberately *not* recursive: WsgiDAV moves or deletes children one by
        one, so cascading here would delete rows the caller is still iterating
        over.  Rows orphaned by a collection delete can be inspected through the
        ``ref_url`` column (see the index in :func:`ensure_schema`).
        """
        with db.connect(self.settings) as conn:
            db.execute(
                conn,
                f"DELETE FROM `{self.table}` WHERE `ref_hash` = %s",
                (_digest(ref_url),),
            )


class _UrlMapping(MutableMapping):
    """The ``dict`` that WsgiDAV's ``PropertyManager`` expects, over a store."""

    def __init__(self, store):
        self._store = store

    def __getitem__(self, ref_url):
        props = self._store.get(ref_url)
        if not props:
            raise KeyError(ref_url)
        return props

    def __setitem__(self, ref_url, props):
        self._store.replace(ref_url, dict(props))

    def __delitem__(self, ref_url):
        self._store.drop(ref_url)

    def __iter__(self):
        return iter(self._store.urls())

    def __len__(self):
        return self._store.count()


class MariaDBPropertyManager(PropertyManager):
    """``PropertyManager`` whose dictionary lives in MariaDB."""

    def __init__(self, store):
        super().__init__()
        self._store = store

    def __repr__(self):
        return f"MariaDBPropertyManager({self._store!r})"

    def _lazy_open(self):
        self._lock.acquire_write()
        try:
            self._dict = _UrlMapping(self._store)
            self._loaded = True
        finally:
            self._lock.release()

    def _sync(self):
        # Every mutation is already durable; nothing to flush.
        pass


# --------------------------------------------------------------------------- #
# locks
# --------------------------------------------------------------------------- #
class MariaDBLockStore:
    """The lock dictionary as rows of JSON, one row per key."""

    def __init__(self, settings, table="dav_locks"):
        self.settings = settings
        self.table = db.validate_identifier(table, "locks table")

    def __repr__(self):
        return f"MariaDBLockStore({self.table!r})"

    def load(self) -> dict:
        with db.connect(self.settings) as conn:
            rows = db.fetch_all(
                conn, f"SELECT `lock_key`, `lock_value` FROM `{self.table}`"
            )
        locks = {}
        for row in rows:
            try:
                locks[row["lock_key"]] = _decode_value(json.loads(row["lock_value"]))
            except (TypeError, ValueError):
                logger.warning("dropping unreadable lock row %r", row["lock_key"])
        return locks

    def save(self, locks: dict) -> None:
        with db.connect(self.settings) as conn:
            conn.begin()
            try:
                db.execute(conn, f"DELETE FROM `{self.table}`")
                for key, value in locks.items():
                    db.execute(
                        conn,
                        f"INSERT INTO `{self.table}` (`lock_key`, `lock_value`) VALUES (%s, %s)",
                        (key, json.dumps(_encode_value(value))),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def clear(self) -> None:
        with db.connect(self.settings) as conn:
            db.execute(conn, f"DELETE FROM `{self.table}`")

    def count(self) -> int:
        with db.connect(self.settings) as conn:
            row = db.fetch_one(conn, f"SELECT COUNT(*) AS n FROM `{self.table}`")
        return int(row["n"]) if row else 0


class MariaDBLockStorage(LockStorageDict):
    """``LockStorageDict`` that keeps its dictionary in MariaDB.

    WsgiDAV re-assigns dictionary members and calls :meth:`_flush` after every
    mutation, so writing the whole (small) dictionary back is enough - that is
    exactly what the stock shelve implementation does.
    """

    def __init__(self, store, time_source=time.time):
        super().__init__()
        self._store = store
        self._now = time_source

    def __repr__(self):
        return f"MariaDBLockStorage({self._store!r})"

    def open(self):
        super().open()  # asserts the storage is closed and sets self._dict = {}
        self._dict = self._store.load()
        self.cleanup()

    def _flush(self):
        if self._dict is None:
            return
        self._store.save(dict(self._dict))

    def clear(self):
        super().clear()
        self._store.clear()

    def cleanup(self):
        """Drop expired locks (and the URL references pointing at them)."""
        if self._dict is None:
            return
        now = self._now()
        removed = 0
        for key, value in list(self._dict.items()):
            if isinstance(value, dict) and value.get("expire") and value["expire"] < now:
                del self._dict[key]
                removed += 1
        for key, value in list(self._dict.items()):
            if isinstance(value, list):
                alive = [token for token in value if token in self._dict]
                if alive:
                    self._dict[key] = alive
                else:
                    del self._dict[key]
        if removed:
            logger.info("purged %d expired lock(s)", removed)
            self._flush()


# --------------------------------------------------------------------------- #
# configuration hook
# --------------------------------------------------------------------------- #
def attach(config: dict) -> None:
    """Install the MariaDB-backed managers into a WsgiDAV config dict.

    Call this *after* any ``deepcopy`` of the configuration: the managers hold
    locks and are meant to be shared by reference.
    """
    state_conf = config.get("webdav_dav_state") or {}
    settings = config.get("webdav_db") or {}
    if not settings.get("user"):
        return  # nothing to attach to (e.g. a unit test with a hand-made config)

    if state_conf.get("persist_properties", True):
        table = state_conf.get("properties_table", "dav_properties")
        if _table_usable(settings, table, "dead properties"):
            config["property_manager"] = MariaDBPropertyManager(
                MariaDBPropertyStore(settings, table)
            )
    if state_conf.get("persist_locks", True):
        table = state_conf.get("locks_table", "dav_locks")
        if _table_usable(settings, table, "locks"):
            config["lock_storage"] = MariaDBLockStorage(MariaDBLockStore(settings, table))
