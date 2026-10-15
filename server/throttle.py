# server/throttle.py
"""A small in-memory login throttle.

Repeated failures for the same key (user name + client address) lock that key
out for the length of the observation window.  This is deliberately simple: it
protects a single-process server from online password guessing without adding a
stateful dependency.  Restarting the server clears the counters.
"""

from __future__ import annotations

import threading
import time

__all__ = ["LoginThrottle"]


class LoginThrottle:
    def __init__(self, max_attempts: int = 10, window_seconds: float = 300.0, time_source=time.monotonic):
        self.max_attempts = max(1, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self._now = time_source
        self._lock = threading.Lock()
        self._failures = {}

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

    def is_locked(self, key: str) -> bool:
        now = self._now()
        with self._lock:
            return len(self._prune(key, now)) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        now = self._now()
        with self._lock:
            stamps = self._prune(key, now)
            stamps.append(now)
            self._failures[key] = stamps

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def retry_after(self, key: str) -> float:
        """Seconds until the oldest failure leaves the window (0 if unlocked)."""
        now = self._now()
        with self._lock:
            stamps = self._prune(key, now)
            if len(stamps) < self.max_attempts:
                return 0.0
            return max(0.0, stamps[0] + self.window_seconds - now)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()
