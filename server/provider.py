# server/provider.py
"""A filesystem provider that confines every user to ``/<username>/``.

Rather than mapping every account onto the same URL namespace, each user is
served below their own prefix::

    GET/PROPFIND /alice/...      ->  <WEBDAV_SHARE_DIR>/alice/...
    GET/PROPFIND /bob/...        ->  <WEBDAV_SHARE_DIR>/bob/...

The prefix is part of the request URL, so WsgiDAV's lock manager and dead
property store (both keyed by URL) are automatically per user - two accounts
can never see each other's locks or properties.
"""

from __future__ import annotations

import logging
import os

from wsgidav import util
from wsgidav.dav_error import DAVError, HTTP_FORBIDDEN, HTTP_INTERNAL_ERROR
from wsgidav.fs_dav_provider import FilesystemProvider

from server import storage

logger = logging.getLogger(__name__)

__all__ = ["PerUserFilesystemProvider"]


class PerUserFilesystemProvider(FilesystemProvider):
    """Serve ``<root>/<directory_name(user)>`` and refuse everything else.

    Requests that do not address the authenticated user's own prefix are
    rejected with 403, including attempts to walk out with ``..`` or to use a
    crafted ``Destination`` header.
    """

    def __init__(self, root_folder, *, readonly=False, fs_opts=None):
        super().__init__(root_folder, readonly=readonly, fs_opts=fs_opts)
        #: Directory that contains one sub-directory per user.
        self.base_folder_path = self.root_folder_path

    def __repr__(self):
        rw = "Read-Only" if self.readonly else "Read-Write"
        return (
            f"{self.__class__.__name__} for base path "
            f"{self.base_folder_path!r} ({rw})"
        )

    @staticmethod
    def _request_username(environ):
        username = (environ or {}).get("wsgidav.auth.user_name")
        if not username:
            raise DAVError(HTTP_FORBIDDEN, "Authentication is required.")
        return username

    def _user_root(self, username: str, *, create: bool) -> str:
        user_root = os.path.abspath(
            os.path.join(self.base_folder_path, storage.directory_name(username))
        )
        if os.path.isdir(user_root):
            return user_root
        if os.path.exists(user_root):
            raise DAVError(
                HTTP_INTERNAL_ERROR,
                "The storage path exists but is not a directory.",
            )
        if not create:
            return user_root
        try:
            os.makedirs(user_root, exist_ok=True)
        except OSError as exc:
            logger.exception("cannot create storage directory %r", user_root)
            raise DAVError(
                HTTP_INTERNAL_ERROR, f"Cannot create storage directory: {exc}"
            ) from exc
        logger.info("created storage directory for user %r: %s", username, user_root)
        return user_root

    def _loc_to_file_path(self, path, environ=None):
        """Map a DAV path to a file path inside the caller's own directory."""
        username = self._request_username(environ)
        folder = storage.directory_name(username)

        parts = [part for part in (path or "").split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise DAVError(HTTP_FORBIDDEN, "Invalid path.")

        if not parts or parts[0] != folder:
            raise DAVError(
                HTTP_FORBIDDEN,
                f"Access is restricted to /{folder}/ - configure the client "
                f"with http://<host>:<port>/{folder}/",
            )

        user_root = self._user_root(username, create=True)
        file_path = os.path.abspath(os.path.join(user_root, *parts[1:]))
        if file_path != user_root and not file_path.startswith(user_root + os.sep):
            raise DAVError(HTTP_FORBIDDEN, "Path escapes the user's storage.")

        return util.to_unicode_safe(file_path)
