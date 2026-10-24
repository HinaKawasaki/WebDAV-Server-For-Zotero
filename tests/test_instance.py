# tests/test_instance.py
"""The single-instance file lock (server/instance.py)."""
import os
import tempfile
import unittest
from pathlib import Path

from server import instance


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "server.lock"

    def test_acquire_creates_the_file_and_writes_the_pid(self):
        lock = instance.acquire(self.path)
        try:
            self.assertTrue(self.path.exists())
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        finally:
            lock.release()

    def test_a_second_instance_is_rejected(self):
        first = instance.acquire(self.path)
        try:
            with self.assertRaises(instance.AlreadyRunningError) as ctx:
                instance.acquire(self.path)
            self.assertIn(str(os.getpid()), str(ctx.exception))
        finally:
            first.release()

    def test_releasing_allows_a_new_acquire(self):
        first = instance.acquire(self.path)
        first.release()
        second = instance.acquire(self.path)
        second.release()

    def test_a_stale_file_does_not_block(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("999999\n")
        lock = instance.acquire(self.path)
        try:
            self.assertEqual(self.path.read_text().strip(), str(os.getpid()))
        finally:
            lock.release()

    def test_context_manager_releases(self):
        with instance.acquire(self.path):
            pass
        instance.acquire(self.path).release()

    def test_release_is_idempotent(self):
        lock = instance.acquire(self.path)
        lock.release()
        lock.release()


if __name__ == "__main__":
    unittest.main()
