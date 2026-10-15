# tests/test_migration.py
import tempfile
import unittest
from pathlib import Path

from server.migration import apply_migration, plan_migration


class MigrationPlanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.legacy = self.root / "zotero"
        self.legacy.mkdir()
        (self.legacy / "library.sqlite").write_text("db")
        (self.legacy / "storage").mkdir()
        (self.legacy / "storage" / "item.zip").write_text("zip")
        self.destination = self.legacy / "alice"  # inside the legacy root

    def tearDown(self):
        self._tmp.cleanup()

    def test_plan_lists_entries_and_skips_destination(self):
        plan = plan_migration(self.legacy, self.destination)
        names = sorted(entry.name for entry in plan.entries)
        self.assertEqual(names, ["library.sqlite", "storage"])
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(plan.action, "move")

    def test_plan_creates_destination(self):
        self.assertFalse(self.destination.exists())
        plan_migration(self.legacy, self.destination)
        self.assertTrue(self.destination.is_dir())

    def test_plan_detects_conflicts(self):
        self.destination.mkdir()
        (self.destination / "library.sqlite").write_text("old")
        plan = plan_migration(self.legacy, self.destination)
        self.assertEqual(plan.conflicts, ["library.sqlite"])

    def test_plan_rejects_missing_source(self):
        with self.assertRaises(ValueError):
            plan_migration(self.legacy / "nope", self.destination)

    def test_plan_rejects_same_source_and_destination(self):
        with self.assertRaises(ValueError):
            plan_migration(self.legacy, self.legacy)

    def test_apply_move(self):
        plan = plan_migration(self.legacy, self.destination)
        apply_migration(plan)
        self.assertTrue((self.destination / "library.sqlite").is_file())
        self.assertTrue((self.destination / "storage" / "item.zip").is_file())
        self.assertFalse((self.legacy / "library.sqlite").exists())

    def test_apply_copy_keeps_the_source(self):
        plan = plan_migration(self.legacy, self.destination, copy=True)
        apply_migration(plan)
        self.assertTrue((self.destination / "library.sqlite").is_file())
        self.assertTrue((self.legacy / "library.sqlite").is_file())
        self.assertEqual(plan.action, "copy")

    def test_apply_overwrites_existing_entries(self):
        self.destination.mkdir()
        (self.destination / "library.sqlite").write_text("old")
        plan = plan_migration(self.legacy, self.destination)
        apply_migration(plan)
        self.assertEqual(
            (self.destination / "library.sqlite").read_text(), "db"
        )

    def test_describe_marks_overwrites(self):
        self.destination.mkdir()
        (self.destination / "library.sqlite").write_text("old")
        plan = plan_migration(self.legacy, self.destination)
        lines = list(plan.describe())
        self.assertIn("move library.sqlite (overwrite)", lines)


if __name__ == "__main__":
    unittest.main()
