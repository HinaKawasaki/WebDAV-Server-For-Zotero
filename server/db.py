# server/db.py
"""MariaDB / MySQL access layer.

A connection is opened for a single unit of work and always closed again:
:func:`connect` is a context manager, so connections cannot leak even when a
statement raises.
"""

from __future__ import annotations

import contextlib
import logging
import re

import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger(__name__)

__all__ = [
    "DatabaseError",
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


@contextlib.contextmanager
def connect(settings):
    """Yield a live connection and always close it afterwards."""
    host = settings.get("host", "127.0.0.1")
    port = int(settings.get("port", 3306))
    try:
        conn = pymysql.connect(
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

    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


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
