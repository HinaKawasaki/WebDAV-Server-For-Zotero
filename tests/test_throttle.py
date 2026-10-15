# tests/test_throttle.py
import unittest

from server.throttle import LoginThrottle


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.throttle = LoginThrottle(
            max_attempts=3, window_seconds=60, time_source=self.clock
        )

    def test_not_locked_initially(self):
        self.assertFalse(self.throttle.is_locked("k"))
        self.assertEqual(self.throttle.retry_after("k"), 0.0)

    def test_locks_after_max_attempts(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.assertTrue(self.throttle.is_locked("k"))
        self.assertGreater(self.throttle.retry_after("k"), 0.0)

    def test_keys_are_independent(self):
        for _ in range(3):
            self.throttle.record_failure("a")
        self.assertTrue(self.throttle.is_locked("a"))
        self.assertFalse(self.throttle.is_locked("b"))

    def test_reset_unlocks(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.throttle.reset("k")
        self.assertFalse(self.throttle.is_locked("k"))

    def test_window_expiry_unlocks(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.assertTrue(self.throttle.is_locked("k"))
        self.clock.advance(61)
        self.assertFalse(self.throttle.is_locked("k"))
        self.assertEqual(self.throttle.retry_after("k"), 0.0)

    def test_old_failures_do_not_count(self):
        self.throttle.record_failure("k")
        self.throttle.record_failure("k")
        self.clock.advance(61)
        self.throttle.record_failure("k")
        self.assertFalse(self.throttle.is_locked("k"))

    def test_max_attempts_is_at_least_one(self):
        self.assertEqual(LoginThrottle(max_attempts=0).max_attempts, 1)


if __name__ == "__main__":
    unittest.main()
