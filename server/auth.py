# server/auth.py
"""WsgiDAV domain controller backed by MariaDB / MySQL.

The database settings and the realm are read from the WsgiDAV configuration
dict that :func:`server.app.create_dav_app` passes in, so this class holds no
global or class-level state.
"""

from __future__ import annotations

import logging

from wsgidav.dc.base_dc import BaseDomainController

from server import users
from server.db import DatabaseError
from server.throttle import LoginThrottle

logger = logging.getLogger(__name__)

__all__ = ["MariaDBDomainController"]


class MariaDBDomainController(BaseDomainController):
    def __init__(self, wsgidav_app, config):
        super().__init__(wsgidav_app, config)

        self.db_settings = config.get("webdav_db") or {}
        auth_conf = config.get("webdav_auth") or {}
        self.realm = auth_conf.get("realm") or "Zotero WebDAV Storage"
        self.throttle = LoginThrottle(
            max_attempts=auth_conf.get("login_max_attempts", 10),
            window_seconds=auth_conf.get("login_window_seconds", 300),
        )

    def __str__(self):
        return f"{self.__class__.__name__}(realm={self.realm!r})"

    def get_domain_realm(self, path_info, environ):
        return self.realm

    def require_authentication(self, realm, environ):
        # Every request must authenticate; there is no anonymous access.
        return True

    def supports_http_digest_auth(self):
        return False

    @staticmethod
    def _client_key(user_name, environ):
        address = ""
        if isinstance(environ, dict):
            address = environ.get("REMOTE_ADDR") or ""
        return f"{user_name or ''}\x00{address}"

    def basic_auth_user(self, realm, user_name, password, environ) -> bool:
        key = self._client_key(user_name, environ)

        if self.throttle.is_locked(key):
            retry_after = self.throttle.retry_after(key)
            logger.warning(
                "basic auth blocked by throttle for user %r from %s (retry in %.0fs)",
                user_name,
                (environ or {}).get("REMOTE_ADDR", "?"),
                retry_after,
            )
            return False

        try:
            ok = users.authenticate(self.db_settings, user_name, password)
        except DatabaseError:
            # Fail closed, but say why: this is not a wrong password.
            logger.exception(
                "authentication backend unavailable for user %r", user_name
            )
            return False

        if ok:
            self.throttle.reset(key)
            return True

        self.throttle.record_failure(key)
        logger.warning(
            "authentication failed for user %r from %s",
            user_name,
            (environ or {}).get("REMOTE_ADDR", "?"),
        )
        return False
