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
from server.authcache import AuthCache
from server.db import DatabaseError
from server.mw import RETRY_AFTER_ENVIRON_KEY, UPSTREAM_UNAVAILABLE_ENVIRON_KEY
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
            base_penalty=auth_conf.get("login_base_penalty", 2),
            max_penalty=auth_conf.get("login_max_penalty", 300),
            max_entries=auth_conf.get("login_max_keys", 4096),
        )
        cache_conf = config.get("webdav_auth_cache") or {}
        self.cache = AuthCache(
            ttl_seconds=cache_conf.get("ttl_seconds", 0),
            max_entries=cache_conf.get("max_entries", 512),
            epoch_file=cache_conf.get("epoch_file"),
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

        # A verification that succeeded moments ago is served from the cache:
        # no database round trip, no scrypt - and no penalty.
        if self.cache.check(user_name, password):
            self.throttle.reset(key)
            return True

        try:
            ok = users.authenticate(self.db_settings, user_name, password)
        except DatabaseError:
            # Fail closed, but make it visible as a *server* problem.  A plain
            # 401 here would look exactly like a wrong password to the client.
            logger.exception(
                "authentication backend unavailable for user %r", user_name
            )
            if isinstance(environ, dict):
                environ[UPSTREAM_UNAVAILABLE_ENVIRON_KEY] = 5
            return False

        if ok:
            # Throttling never blocks a correct password: every client of this
            # service shares one source address (loopback, behind the
            # tunnel/VPN), so the throttle key degenerates to "per account" and
            # blocking would let anybody lock anybody else out by feeding that
            # account wrong passwords.  Wrong guesses still cost a full scrypt
            # verification and are answered with a Retry-After hint below.
            self.throttle.reset(key)
            self.cache.store(user_name, password)
            return True

        self.throttle.record_failure(key)
        retry_after = self.throttle.retry_after(key)
        address = (environ or {}).get("REMOTE_ADDR", "?")
        if retry_after > 0.0:
            if isinstance(environ, dict):
                # RequestGuard turns this into a Retry-After header.
                environ[RETRY_AFTER_ENVIRON_KEY] = retry_after
            logger.warning(
                "authentication failed for user %r from %s (retry in %.0fs)",
                user_name,
                address,
                retry_after,
            )
        else:
            logger.warning(
                "authentication failed for user %r from %s", user_name, address
            )
        return False
