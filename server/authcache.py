# server/authcache.py
"""A short-lived, in-memory cache of *successful* authentications.

Every DAV request carries Basic credentials, so without a cache each request
costs one database round trip plus one scrypt verification - measured at about
46 ms of CPU per request on the production host.  Remembering the outcome of a
verification for a few seconds removes nearly all of that cost during the
bursts of requests a sync client produces.

The trade-offs here are deliberate:

* only successes are cached - a wrong password always pays the full path, so
  the throttle keeps seeing (and counting) failed attempts;
* the key is an HMAC of the password under a per-process random secret, so
  neither the password nor a plain digest of it is retained in memory;
* entries expire after ``ttl_seconds`` (0 disables caching entirely);
* the number of entries is bounded;
* a *generation file* lets ``manage passwd`` / ``manage deluser`` drop every
  entry at once, so a changed password stops working immediately instead of
  lingering until the TTL runs out.

The generation file is ``stat()``-ed on every lookup (one cheap syscall); when
its mtime changes the cache is cleared.  If the file cannot be created or read,
the cache simply falls back to TTL-only invalidation.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["AuthCache", "invalidate"]


def invalidate(epoch_file) -> bool:
    """Bump the cache generation so every running server drops its entries.

    Touching the file is enough: the servers compare its mtime.  Returns True
    when the file exists afterwards.
    """
    if not epoch_file:
        return False
    path = Path(epoch_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:
        logger.warning("could not touch the auth cache generation file %s: %s", path, exc)
        return False
    return True


class AuthCache:
    def __init__(
        self,
        ttl_seconds: float = 45.0,
        max_entries: int = 512,
        epoch_file=None,
        time_source=time.monotonic,
    ):
        self.ttl = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._now = time_source
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._entries: dict = {}
        self._epoch_path = Path(epoch_file) if epoch_file else None
        self._epoch_stamp = self._stat_epoch()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _stat_epoch(self):
        if self._epoch_path is None:
            return None
        try:
            return self._epoch_path.stat().st_mtime_ns
        except OSError:
            return None

    def _drop_if_generation_changed(self) -> None:
        """Caller must hold ``self._lock``."""
        if self._epoch_path is None:
            return
        stamp = self._stat_epoch()
        if stamp == self._epoch_stamp:
            return
        self._epoch_stamp = stamp
        if self._entries:
            logger.info("auth cache invalidated (%s changed)", self._epoch_path)
        self._entries.clear()

    def _digest(self, user_name, password) -> bytes:
        seed = f"{user_name or ''}\x00{password or ''}".encode("utf-8", "surrogatepass")
        return hmac.new(self._secret, seed, hashlib.sha256).digest()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    @property
    def enabled(self) -> bool:
        return self.ttl > 0.0

    def check(self, user_name, password) -> bool:
        """True when this exact combination verified successfully recently."""
        if not self.enabled:
            return False
        key = self._digest(user_name, password)
        now = self._now()
        with self._lock:
            self._drop_if_generation_changed()
            expiry = self._entries.get(key)
            if expiry is None:
                return False
            if expiry <= now:
                del self._entries[key]
                return False
            return True

    def store(self, user_name, password) -> None:
        """Remember a successful verification."""
        if not self.enabled:
            return
        key = self._digest(user_name, password)
        with self._lock:
            self._drop_if_generation_changed()
            self._entries[key] = self._now() + self.ttl
            overflow = len(self._entries) - self.max_entries
            if overflow > 0:
                # drop whatever is closest to expiry
                for stale in sorted(self._entries, key=self._entries.__getitem__)[:overflow]:
                    del self._entries[stale]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
