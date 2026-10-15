# server/storage.py
"""Per-user storage layout.

Every WebDAV account gets its own directory below ``WEBDAV_SHARE_DIR`` and may
only address URLs below ``/<directory_name>/``::

    <WEBDAV_SHARE_DIR>/alice/...
    <WEBDAV_SHARE_DIR>/bob/...

Because the user name is part of the URL, WsgiDAV's lock table and dead
property store - both keyed by the request URL - are separated per user as
well, without any extra bookkeeping.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = [
    "PORTABLE_USERNAME_RE",
    "directory_name",
    "ensure_user_directory",
    "is_portable_username",
    "user_directory",
]

#: User names matching this pattern map 1:1 onto a directory name that is safe
#: on every platform we support.  New accounts are validated against it.
PORTABLE_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}$")

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._@+-]")
_HASH_LENGTH = 8
_MAX_NAME_LENGTH = 64
#: Separator for hashed names.  It can never appear in a portable user name, so
#: the mapping stays injective.
_HASH_SEPARATOR = "~"


def is_portable_username(username: str) -> bool:
    """Return True if ``username`` can be used as a directory name verbatim."""
    return bool(isinstance(username, str) and PORTABLE_USERNAME_RE.match(username))


def directory_name(username: str) -> str:
    """Map a user name to a safe directory name (injective).

    Portable user names are used unchanged.  Anything else (legacy rows, names
    with slashes or spaces, ...) is sanitised and suffixed with a short hash so
    two different accounts can never collide on the same directory.
    """
    if not isinstance(username, str) or not username:
        raise ValueError("username must be a non-empty string")
    if is_portable_username(username):
        return username

    safe = _UNSAFE_RE.sub("_", username).strip("._") or "user"
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
    budget = _MAX_NAME_LENGTH - len(_HASH_SEPARATOR) - len(digest)
    return f"{safe[:budget]}{_HASH_SEPARATOR}{digest}"


def user_directory(base_dir, username: str) -> Path:
    """Return the storage directory of ``username`` (it may not exist yet)."""
    return Path(base_dir) / directory_name(username)


def ensure_user_directory(base_dir, username: str) -> Path:
    """Create the storage directory of ``username`` and return it."""
    path = user_directory(base_dir, username)
    path.mkdir(parents=True, exist_ok=True)
    return path
