# tests/test_authcache.py
import os
import tempfile
import unittest
from pathlib import Path

from server.authcache import AuthCache, invalidate


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class AuthCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.cache = AuthCache(ttl_seconds=45, time_source=self.clock)

    def test_disabled_when_ttl_is_zero(self):
        cache = AuthCache(ttl_seconds=0, time_source=self.clock)
        cache.store("alice", "pw")
        self.assertFalse(cache.check("alice", "pw"))
        self.assertEqual(len(cache), 0)

    def test_hit_after_store(self):
        self.cache.store("alice", "pw")
        self.assertTrue(self.cache.check("alice", "pw"))

    def test_miss_before_store(self):
        self.assertFalse(self.cache.check("alice", "pw"))

    def test_wrong_password_is_a_miss(self):
        self.cache.store("alice", "pw")
        self.assertFalse(self.cache.check("alice", "other"))

    def test_other_user_is_a_miss(self):
        self.cache.store("alice", "pw")
        self.assertFalse(self.cache.check("bob", "pw"))

    def test_entry_expires(self):
        self.cache.store("alice", "pw")
        self.clock.advance(44)
        self.assertTrue(self.cache.check("alice", "pw"))
        self.clock.advance(2)
        self.assertFalse(self.cache.check("alice", "pw"))

    def test_clear_drops_everything(self):
        self.cache.store("alice", "pw")
        self.cache.clear()
        self.assertFalse(self.cache.check("alice", "pw"))

    def test_capacity_is_bounded(self):
        cache = AuthCache(ttl_seconds=60, max_entries=3, time_source=self.clock)
        for index in range(10):
            cache.store("alice", f"pw-{index}")
            self.clock.advance(1)
        self.assertLessEqual(len(cache), 3)
        # the most recent entry must survive
        self.assertTrue(cache.check("alice", "pw-9"))

    def test_password_is_not_retained(self):
        self.cache.store("alice", "s3cret-pw")
        self.assertNotIn("s3cret-pw", repr(self.cache._entries))

    def test_two_caches_do_not_accept_each_others_keys(self):
        other = AuthCache(ttl_seconds=45, time_source=self.clock)
        other.store("alice", "pw")
        self.assertFalse(self.cache.check("alice", "pw"))


class GenerationFileTests(unittest.TestCase):
    def test_touching_the_file_invalidates_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            epoch = Path(tmp) / "generation"
            epoch.touch()
            cache = AuthCache(ttl_seconds=60, epoch_file=epoch)
            cache.store("alice", "pw")
            self.assertTrue(cache.check("alice", "pw"))

            os.utime(epoch, (1, 1))
            self.assertFalse(cache.check("alice", "pw"))

    def test_invalidate_creates_missing_file_and_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            epoch = Path(tmp) / "nested" / "generation"
            self.assertTrue(invalidate(epoch))
            self.assertTrue(epoch.exists())

    def test_missing_generation_file_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = AuthCache(ttl_seconds=60, epoch_file=Path(tmp) / "absent")
            cache.store("alice", "pw")
            self.assertTrue(cache.check("alice", "pw"))

    def test_no_generation_file_configured(self):
        cache = AuthCache(ttl_seconds=60)
        cache.store("alice", "pw")
        self.assertTrue(cache.check("alice", "pw"))
        self.assertFalse(invalidate(None))


if __name__ == "__main__":
    unittest.main()
