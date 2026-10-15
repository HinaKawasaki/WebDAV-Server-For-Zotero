# tests/test_storage.py
import tempfile
import unittest
from pathlib import Path

from server import storage


class DirectoryNameTests(unittest.TestCase):
    def test_portable_names_are_used_verbatim(self):
        for name in ("alice", "a.b-c_d+e@f", "A1", "user123"):
            self.assertTrue(storage.is_portable_username(name), name)
            self.assertEqual(storage.directory_name(name), name)

    def test_unsafe_names_are_not_portable(self):
        for name in ("", ".hidden", "a/b", "a\\b", "..", "a b", "-lead", "a" * 65):
            self.assertFalse(storage.is_portable_username(name), name)

    def test_unsafe_names_are_sanitised_and_injective(self):
        first = storage.directory_name("a/b")
        second = storage.directory_name("a\\b")
        self.assertNotEqual(first, second)
        self.assertNotIn("/", first)
        self.assertNotIn("\\", first)
        self.assertLessEqual(len(first), 64)

    def test_dot_only_names_cannot_escape(self):
        for name in (".", "..", "..."):
            result = storage.directory_name(name)
            self.assertNotIn("..", result)
            self.assertNotEqual(result, name)

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            storage.directory_name("")

    def test_long_name_is_truncated_with_hash(self):
        name = "x" * 200
        result = storage.directory_name(name)
        self.assertLessEqual(len(result), 64)
        self.assertEqual(result, storage.directory_name(name))


class UserDirectoryTests(unittest.TestCase):
    def test_user_directory_path(self):
        self.assertEqual(
            storage.user_directory("/base", "alice"), Path("/base/alice")
        )

    def test_ensure_user_directory_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = storage.ensure_user_directory(tmp, "alice")
            self.assertTrue(path.is_dir())
            self.assertEqual(path, Path(tmp) / "alice")

    def test_ensure_user_directory_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = storage.ensure_user_directory(tmp, "alice")
            (first / "file.txt").write_text("x")
            second = storage.ensure_user_directory(tmp, "alice")
            self.assertEqual(first, second)
            self.assertTrue((second / "file.txt").is_file())


if __name__ == "__main__":
    unittest.main()
