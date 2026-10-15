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
            max_attempts=3,
            window_seconds=60,
            base_penalty=2,
            max_penalty=10,
            time_source=self.clock,
        )

    def test_not_locked_initially(self):
        self.assertFalse(self.throttle.is_locked("k"))
        self.assertEqual(self.throttle.retry_after("k"), 0.0)

    def test_locks_after_max_attempts(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.assertTrue(self.throttle.is_locked("k"))
        self.assertEqual(self.throttle.retry_after("k"), 2.0)

    def test_penalty_doubles_with_each_further_failure(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.assertEqual(self.throttle.retry_after("k"), 2.0)
        self.throttle.record_failure("k")
        self.assertEqual(self.throttle.retry_after("k"), 4.0)
        self.throttle.record_failure("k")
        self.assertEqual(self.throttle.retry_after("k"), 8.0)
        self.throttle.record_failure("k")
        self.assertEqual(self.throttle.retry_after("k"), 10.0)  # capped

    def test_penalty_counts_down_from_the_last_failure(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.clock.advance(1.5)
        self.assertAlmostEqual(self.throttle.retry_after("k"), 0.5)
        self.clock.advance(0.6)
        self.assertFalse(self.throttle.is_locked("k"))

    def test_failure_after_the_penalty_escalates(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.clock.advance(3)
        self.assertFalse(self.throttle.is_locked("k"))
        self.throttle.record_failure("k")
        self.assertEqual(self.throttle.retry_after("k"), 4.0)

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

    def test_window_expiry_clears_history(self):
        for _ in range(3):
            self.throttle.record_failure("k")
        self.clock.advance(61)
        self.assertFalse(self.throttle.is_locked("k"))
        self.assertEqual(self.throttle.retry_after("k"), 0.0)
        # the old failures are gone, so a single new one does not lock
        self.throttle.record_failure("k")
        self.assertFalse(self.throttle.is_locked("k"))

    def test_old_failures_do_not_count(self):
        self.throttle.record_failure("k")
        self.throttle.record_failure("k")
        self.clock.advance(61)
        self.throttle.record_failure("k")
        self.assertFalse(self.throttle.is_locked("k"))

    def test_zero_attempts_disables_throttling(self):
        throttle = LoginThrottle(max_attempts=0, time_source=self.clock)
        for _ in range(50):
            throttle.record_failure("k")
        self.assertFalse(throttle.is_locked("k"))
        self.assertEqual(throttle.retry_after("k"), 0.0)
        self.assertFalse(throttle.enabled)

    def test_max_penalty_is_never_below_the_base(self):
        throttle = LoginThrottle(
            max_attempts=1, base_penalty=5, max_penalty=1, time_source=self.clock
        )
        self.assertGreaterEqual(throttle.max_penalty, throttle.base_penalty)


if __name__ == "__main__":
    unittest.main()
