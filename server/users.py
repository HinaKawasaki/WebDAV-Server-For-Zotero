# server/users.py
"""User store backed by the ``dav_users`` table (MariaDB / MySQL).

The table layout is intentionally compatible with the historical schema::

    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password VARCHAR(255) NOT NULL

Existing rows keep working: :func:`authenticate` accepts legacy plaintext and
unsalted SHA-256 values and rewrites them as salted scrypt on the next
successful login.
"""

from __future__ import annotations

import logging

from server import db, passwords, storage

logger = logging.getLogger(__name__)

__all__ = [
    "UserExistsError",
    "UserNotFoundError",
    "add_user",
    "authenticate",
    "check_schema",
    "count_users",
    "delete_user",
    "ensure_schema",
    "get_stored_password",
    "list_users",
    "set_password",
]

#: ``dav_users.username`` is VARCHAR(50) in existing deployments.
USERNAME_MAX_LENGTH = 50
# scrypt hashes are ~100 chars; keep headroom for future schemes.
PASSWORD_COLUMN_LENGTH = 255

# Verified against when the account does not exist, so that "unknown user" and
# "wrong password" cost the same and cannot be told apart by timing.
_DUMMY_HASH = passwords.hash_password("timing-equalizer-not-a-real-password")


class UserExistsError(ValueError):
    """Raised when adding a user name that is already taken."""


class UserNotFoundError(ValueError):
    """Raised when a user name is not present in the database."""


def _table(settings) -> str:
    return db.validate_identifier(settings["table"], "table name")


def _check_username(username: str) -> str:
    """Validate a user name for lookups and updates.

    Legacy rows may contain characters that are not usable in a URL or a
    directory name; those accounts must still be able to log in, change their
    password and be deleted.
    """
    if not isinstance(username, str):
        raise ValueError("username must be a string")
    if not username:
        raise ValueError("username must not be empty")
    if len(username) > USERNAME_MAX_LENGTH:
        raise ValueError(
            f"username must be at most {USERNAME_MAX_LENGTH} characters"
        )
    if any(ord(char) < 32 for char in username):
        raise ValueError("username must not contain control characters")
    return username


def _check_new_username(username: str) -> str:
    """Validate a user name for creation.

    Each account is served below ``/<username>/``, so new accounts additionally
    have to be usable as a URL path segment and as a directory name.
    """
    username = _check_username(username)
    if not storage.is_portable_username(username):
        raise ValueError(
            "username may only contain letters, digits and '._@+-', and must "
            "start with a letter or a digit"
        )
    return username


#: MySQL/MariaDB error codes meaning "this account may not do that".
_PERMISSION_ERROR_CODES = frozenset({1044, 1142})


def _table_exists(settings, table: str) -> bool:
    try:
        with db.connect(settings) as conn:
            db.fetch_one(conn, f"SELECT 1 FROM `{table}` LIMIT 1")
        return True
    except db.DatabaseError:
        return False


def ensure_schema(settings) -> None:
    """Make sure the user table exists and is usable.

    A deployment may deliberately run without the ``CREATE`` privilege (the
    table is created once by a DBA).  In that case an existing table is
    accepted; only a genuinely missing table is an error.
    """
    table = _table(settings)
    try:
        with db.connect(settings) as conn:
            db.execute(
                conn,
                f"""
                CREATE TABLE IF NOT EXISTS `{table}` (
                    `id` INT NOT NULL AUTO_INCREMENT,
                    `username` VARCHAR({USERNAME_MAX_LENGTH}) NOT NULL,
                    `password` VARCHAR({PASSWORD_COLUMN_LENGTH}) NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uq_{table}_username` (`username`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin
                """,
            )
    except db.DatabaseError as exc:
        if exc.code not in _PERMISSION_ERROR_CODES:
            raise
        logger.warning(
            "no privilege to create %r (%s); checking the existing table", table, exc
        )
        if not _table_exists(settings, table):
            raise db.DatabaseError(
                f"table {table!r} does not exist and this account may not create "
                f"it - create it manually or grant CREATE"
            ) from exc

    logger.info("user table %r is ready", table)


def check_schema(settings) -> dict:
    """Inspect the table and report whether stored hashes can fit.

    Returns a dict with the observed column widths and a list of warnings.
    Raises :class:`server.db.DatabaseError` when the ``password`` column is too
    narrow for the current hash format, because creating or migrating an
    account would then fail at runtime.
    """
    table = _table(settings)
    required = passwords.hash_length()
    info = {
        "table": table,
        "username_column": None,
        "password_column": None,
        "required_password_length": required,
        "warnings": [],
    }

    try:
        with db.connect(settings) as conn:
            rows = db.fetch_all(
                conn,
                """
                SELECT COLUMN_NAME, CHARACTER_MAXIMUM_LENGTH
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                """,
                (table,),
            )
    except db.DatabaseError as exc:
        # Some accounts cannot read information_schema; the check is advisory.
        logger.debug("schema introspection unavailable: %s", exc)
        return info

    widths = {
        row["COLUMN_NAME"]: row["CHARACTER_MAXIMUM_LENGTH"] for row in rows or []
    }
    info["username_column"] = widths.get("username")
    info["password_column"] = widths.get("password")

    password_width = info["password_column"]
    if password_width is not None and password_width < required:
        database = settings.get("database") or ""
        qualified = f"`{database}`.`{table}`" if database else f"`{table}`"
        raise db.DatabaseError(
            f"column `{table}`.`password` is VARCHAR({password_width}) but the "
            f"current hash format needs {required} characters. Widen it first:\n"
            f"    ALTER TABLE {qualified} MODIFY `password` "
            f"VARCHAR({PASSWORD_COLUMN_LENGTH}) NOT NULL;"
        )
    if password_width is not None and password_width < PASSWORD_COLUMN_LENGTH:
        info["warnings"].append(
            f"`{table}`.`password` is VARCHAR({password_width}), which fits the "
            f"current {required}-character hashes but leaves little room for "
            f"future changes"
        )
    return info


def count_users(settings) -> int:
    table = _table(settings)
    with db.connect(settings) as conn:
        row = db.fetch_one(conn, f"SELECT COUNT(*) AS n FROM `{table}`")
    return int(row["n"]) if row else 0


def list_users(settings):
    table = _table(settings)
    with db.connect(settings) as conn:
        rows = db.fetch_all(
            conn, f"SELECT `username` FROM `{table}` ORDER BY `username`"
        )
    return [row["username"] for row in rows]


def get_stored_password(settings, username: str):
    table = _table(settings)
    with db.connect(settings) as conn:
        row = db.fetch_one(
            conn,
            f"SELECT `password` FROM `{table}` WHERE `username` = %s LIMIT 1",
            (username,),
        )
    return row["password"] if row else None


def add_user(settings, username: str, password: str) -> None:
    username = _check_new_username(username)
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    table = _table(settings)
    digest = passwords.hash_password(password)
    with db.connect(settings) as conn:
        try:
            db.execute(
                conn,
                f"INSERT INTO `{table}` (`username`, `password`) VALUES (%s, %s)",
                (username, digest),
            )
        except db.DatabaseError as exc:
            if "Duplicate entry" in str(exc) or "1062" in str(exc):
                raise UserExistsError(f"user {username!r} already exists") from exc
            raise
    logger.info("created user %r", username)


def set_password(settings, username: str, password: str) -> None:
    username = _check_username(username)
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    table = _table(settings)
    digest = passwords.hash_password(password)
    with db.connect(settings) as conn:
        affected = db.execute(
            conn,
            f"UPDATE `{table}` SET `password` = %s WHERE `username` = %s",
            (digest, username),
        )
    if not affected:
        raise UserNotFoundError(f"user {username!r} does not exist")


def delete_user(settings, username: str) -> bool:
    username = _check_username(username)
    table = _table(settings)
    with db.connect(settings) as conn:
        affected = db.execute(
            conn, f"DELETE FROM `{table}` WHERE `username` = %s", (username,)
        )
    return bool(affected)


def authenticate(settings, username: str, password: str) -> bool:
    """Return True if the credentials are valid.

    Raises :class:`server.db.DatabaseError` when the backend itself fails, so
    callers can distinguish "wrong password" from "database is down".
    """
    if (
        not isinstance(username, str)
        or not username
        or len(username) > USERNAME_MAX_LENGTH
        or not isinstance(password, str)
        or not password
    ):
        # Still burn a hash so the failure path stays constant-time-ish.
        passwords.verify_password(password if isinstance(password, str) else "", _DUMMY_HASH)
        return False

    stored = get_stored_password(settings, username)
    if stored is None:
        passwords.verify_password(password, _DUMMY_HASH)
        return False

    ok, needs_rehash = passwords.verify_password(password, stored)
    if not ok:
        return False

    if needs_rehash:
        try:
            set_password(settings, username, password)
            logger.info("upgraded stored password of %r to scrypt", username)
        except (db.DatabaseError, UserNotFoundError):
            # A failed upgrade must never turn a valid login into a failure.
            logger.warning(
                "could not upgrade stored password of %r", username, exc_info=True
            )
    return True
