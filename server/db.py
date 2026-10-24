# server/db.py
"""MariaDB / MySQL access layer.

:func:`connect` is a context manager that hands out a live connection.  Instead
of opening (and closing) one for every statement it keeps a small pool of
connections **per thread** - cheroot runs a fixed thread pool, so a connection
is reused by every request that thread serves.

That matters a lot for this service: authentication and every dead-property
lookup go through the database, and a fresh connection costs a TCP handshake,
a MySQL handshake and an authentication round trip each time.  Measured on a
forwarded database with ~28 ms RTT, one connection setup took ~166 ms while a
query on a reused connection took ~28 ms.

Pooled connections are checked out without a round trip and only ``ping()``ed
after ``_POOL_IDLE_SECONDS`` of inactivity, so a stable client pays no health
check cost.  A connection that turns out to be broken is dropped and the next
call opens a fresh one; ``DatabaseError`` carrying a connection-level error code
also evicts it.  :func:`close_pooled` closes this thread's connections (tests
and orderly shutdown).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time

import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger(__name__)

__all__ = [
    "DatabaseError",
    "close_pooled",
    "connect",
    "execute",
    "fetch_all",
    "fetch_one",
    "ping",
    "validate_identifier",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DatabaseError(RuntimeError):
    """Raised when the database is unreachable or a statement fails.

    :attr:`code` carries the numeric MySQL/MariaDB error code when there is
    one, so callers can react to specific conditions (e.g. a missing ``CREATE``
    privilege) without parsing the message.
    """

    def __init__(self, message: str, code=None):
        super().__init__(message)
        self.code = code


def _error_code(exc) -> int:
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _wrap(exc, action: str) -> DatabaseError:
    return DatabaseError(f"{action} failed: {exc}", code=_error_code(exc))


def validate_identifier(name: str, what: str = "identifier") -> str:
    """Validate a SQL identifier that has to be interpolated into a statement.

    Table names cannot be passed as query parameters in MySQL, so the only safe
    approach is a strict allow-list.
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name) or len(name) > 64:
        raise DatabaseError(f"Invalid {what}: {name!r}")
    return name


#: A pooled connection is health-checked after this many seconds of inactivity.
_POOL_IDLE_SECONDS = 10.0
#: Per-thread upper bound (the server only ever talks to one database).
_POOL_MAX_PER_THREAD = 4
#: MySQL error codes that mean "this connection is no longer usable".
_CONNECTION_ERROR_CODES = frozenset({2006, 2013, 2055})

_POOL = threading.local()


def _pool_key(settings) -> tuple:
    return (
        settings.get("host", "127.0.0.1"),
        int(settings.get("port", 3306)),
        settings.get("user", ""),
        settings.get("password", "") or "",
        settings.get("database", ""),
    )


def _pool_conns() -> dict:
    conns = getattr(_POOL, "conns", None)
    if conns is None:
        conns = {}
        _POOL.conns = conns
    return conns


def _close_quietly(conn) -> None:
    with contextlib.suppress(Exception):
        conn.close()


def close_pooled() -> None:
    """Close every database connection this thread has pooled."""
    conns = getattr(_POOL, "conns", None)
    if not conns:
        return
    for conn, _stamp in conns.values():
        _close_quietly(conn)
    conns.clear()


def _open(settings):
    host = settings.get("host", "127.0.0.1")
    port = int(settings.get("port", 3306))
    try:
        return pymysql.connect(
            host=host,
            port=port,
            user=settings.get("user", ""),
            password=settings.get("password", "") or "",
            database=settings.get("database", ""),
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=int(settings.get("connect_timeout", 5)),
            read_timeout=int(settings.get("read_timeout", 15)),
            write_timeout=int(settings.get("write_timeout", 15)),
        )
    except pymysql.MySQLError as exc:
        raise DatabaseError(
            f"Cannot connect to MySQL/MariaDB at {host}:{port}: {exc}",
            code=_error_code(exc),
        ) from exc


def _drop(key: tuple, conn) -> None:
    _pool_conns().pop(key, None)
    _close_quietly(conn)


def _checkout(settings, key: tuple):
    entry = _pool_conns().get(key)
    if entry is None:
        return _open(settings)

    conn, last_used = entry
    if time.monotonic() - last_used > _POOL_IDLE_SECONDS:
        try:
            conn.ping(reconnect=True)
        except pymysql.MySQLError:
            logger.info("dropping a stale pooled database connection")
            _drop(key, conn)
            return _open(settings)
    return conn


def _checkin(key: tuple, conn) -> None:
    conns = _pool_conns()
    conns.pop(key, None)  # refresh the recency order
    conns[key] = (conn, time.monotonic())
    while len(conns) > _POOL_MAX_PER_THREAD:
        oldest = next(iter(conns))
        _close_quietly(conns.pop(oldest)[0])


@contextlib.contextmanager
def connect(settings, *, reuse: bool = True):
    """Yield a live connection, reusing this thread's pooled one when possible.

    The connection goes back to the pool on success and is discarded when the
    block raises, so a broken or half-used connection can never be handed to
    the next request.
    """
    key = _pool_key(settings)
    conn = _checkout(settings, key) if reuse else _open(settings)

    try:
        yield conn
    except BaseException as exc:
        with contextlib.suppress(Exception):
            conn.rollback()
        if not reuse:
            _close_quietly(conn)
        elif (
            isinstance(exc, DatabaseError)
            and exc.code is not None
            and exc.code not in _CONNECTION_ERROR_CODES
        ):
            # e.g. "duplicate entry": the connection itself is still healthy
            _checkin(key, conn)
        else:
            _drop(key, conn)
        raise
    else:
        if reuse:
            _checkin(key, conn)
        else:
            _close_quietly(conn)


def fetch_one(conn, sql: str, params=None):
    """Run ``sql`` and return the first row (a dict) or None."""
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()
    except pymysql.MySQLError as exc:
        raise _wrap(exc, "Query") from exc


def fetch_all(conn, sql: str, params=None):
    """Run ``sql`` and return all rows (a list of dicts)."""
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())
    except pymysql.MySQLError as exc:
        raise _wrap(exc, "Query") from exc


def execute(conn, sql: str, params=None) -> int:
    """Run a statement and return the number of affected rows."""
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount
    except pymysql.MySQLError as exc:
        raise _wrap(exc, "Statement") from exc


def ping(settings) -> None:
    """Raise :class:`DatabaseError` unless a trivial query succeeds."""
    with connect(settings) as conn:
        fetch_one(conn, "SELECT 1 AS ok")
