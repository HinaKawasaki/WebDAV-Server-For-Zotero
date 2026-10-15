# server/throttle.py
"""A small in-memory login throttle with an exponential penalty box.

After ``max_attempts`` failures for the same key (user name + client address)
the key must wait a penalty that *doubles* with every further failure, capped
at ``max_penalty`` seconds.  A successful authentication clears the key.

Why not "lock out for the rest of the window": behind a single tunnel (or a
VPN) every client shares one source address, so the key degenerates into "per
account".  A stale client retrying an old password would then lock the
legitimate user out for the whole window.  With escalating penalties a few
mistyped attempts cost a couple of seconds, while a sustained guessing run
still ends up waiting minutes between attempts.

``max_attempts=0`` disables throttling entirely (the caller decides).

State is per process; restarting the server clears it.
"""

from __future__ import annotations

import threading
import time

__all__ = ["LoginThrottle"]

DEFAULT_BASE_PENALTY = 2.0
DEFAULT_MAX_PENALTY = 300.0


class LoginThrottle:
    def __init__(
        self,
        max_attempts: int = 10,
        window_seconds: float = 300.0,
        base_penalty: float = DEFAULT_BASE_PENALTY,
        max_penalty: float = DEFAULT_MAX_PENALTY,
        time_source=time.monotonic,
    ):
        self.max_attempts = max(0, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self.base_penalty = max(0.1, float(base_penalty))
        self.max_penalty = max(self.base_penalty, float(max_penalty))
        self._now = time_source
        self._lock = threading.Lock()
        self._failures = {}

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 0

    def _prune(self, key, now):
        stamps = self._failures.get(key)
        if not stamps:
            return []
        cutoff = now - self.window_seconds
        stamps = [stamp for stamp in stamps if stamp > cutoff]
        if stamps:
            self._failures[key] = stamps
        else:
            self._failures.pop(key, None)
        return stamps

    def _penalty(self, failures: int) -> float:
        """The wait required after ``failures`` recorded failures (0 = none)."""
        excess = failures - self.max_attempts + 1
        if excess < 1:
            return 0.0
        return min(self.base_penalty * (2.0 ** (excess - 1)), self.max_penalty)

    def retry_after(self, key: str) -> float:
        """Seconds the caller should wait before trying again (0 = go ahead)."""
        if not self.enabled:
            return 0.0
        now = self._now()
        with self._lock:
            stamps = self._prune(key, now)
            if not stamps:
                return 0.0
            penalty = self._penalty(len(stamps))
            if penalty <= 0.0:
                return 0.0
            return max(0.0, stamps[-1] + penalty - now)

    def is_locked(self, key: str) -> bool:
        return self.retry_after(key) > 0.0

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            stamps = self._prune(key, now)
            stamps.append(now)
            self._failures[key] = stamps

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()
