# server/throttle.py
"""A small in-memory login throttle with an exponential back-off.

After ``max_attempts`` failures for the same key (user name + client address)
the recommended wait *doubles* with every further failure, capped at
``max_penalty`` seconds.  The domain controller hands that value to the client
as ``Retry-After`` so well-behaved clients back off; a successful
authentication clears the key.

The throttle deliberately does **not** refuse to verify credentials.  Behind one
tunnel/VPN every client shares a single source address, so the key degenerates
into "per account": refusing would let anybody lock an account out by feeding it
wrong passwords.  Guessing still costs a scrypt verification per attempt (the
same cost that applies before the threshold), and the throttle adds the
back-off hint and the failure history on top.  Password strength (use
``--generate``) and rate limiting in front of the tunnel are what actually bound
a brute-force run.

``max_attempts=0`` disables throttling entirely (the caller decides).

State is per process; restarting the server clears it.  Both the number of
tracked keys and the number of failures kept per key are bounded, so a client
that keeps sending failures for random user names cannot grow the process
without limit.  When the key budget is exhausted, keys with the fewest failures
are dropped first: accounts that are actually under attack keep their history,
one-shot names do not.
"""

from __future__ import annotations

import threading
import time

__all__ = ["LoginThrottle"]

DEFAULT_BASE_PENALTY = 2.0
DEFAULT_MAX_PENALTY = 300.0

#: How many timestamps are kept per key.  Far more than the few that determine
#: the (capped) penalty, but bounded so one key cannot grow a huge list.
MAX_STAMPS_PER_KEY = 64
#: Default upper bound on tracked keys (user + address pairs).
DEFAULT_MAX_ENTRIES = 4096


class LoginThrottle:
    def __init__(
        self,
        max_attempts: int = 10,
        window_seconds: float = 300.0,
        base_penalty: float = DEFAULT_BASE_PENALTY,
        max_penalty: float = DEFAULT_MAX_PENALTY,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        time_source=time.monotonic,
    ):
        self.max_attempts = max(0, int(max_attempts))
        self.window_seconds = max(1.0, float(window_seconds))
        self.base_penalty = max(0.1, float(base_penalty))
        self.max_penalty = max(self.base_penalty, float(max_penalty))
        self.max_entries = max(1, int(max_entries))
        self._now = time_source
        self._lock = threading.Lock()
        self._failures = {}

    def __len__(self) -> int:
        """Number of keys currently tracked."""
        with self._lock:
            return len(self._failures)

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
            if len(stamps) > MAX_STAMPS_PER_KEY:
                stamps = stamps[-MAX_STAMPS_PER_KEY:]
            self._failures[key] = stamps
            self._shrink(now)

    def _shrink(self, now) -> None:
        """Keep the number of tracked keys bounded.

        Caller must hold ``self._lock``.  Expired keys are dropped first; if
        that is not enough, the keys with the fewest recorded failures go
        (a random user name that failed once is far less interesting than an
        account that is being attacked).
        """
        if len(self._failures) <= self.max_entries:
            return
        cutoff = now - self.window_seconds
        for stale in [k for k, v in self._failures.items() if v[-1] <= cutoff]:
            del self._failures[stale]
        while len(self._failures) > self.max_entries:
            victim = min(
                self._failures,
                key=lambda k: (len(self._failures[k]), self._failures[k][-1]),
            )
            del self._failures[victim]

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._failures.clear()
