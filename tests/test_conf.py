# tests/test_conf.py
import os
import unittest
from unittest import mock

from server import conf


class ParseDotenvTests(unittest.TestCase):
    def test_basic(self):
        parsed = conf.parse_dotenv("A=1\nB = two\n")
        self.assertEqual(parsed, {"A": "1", "B": "two"})

    def test_comments_and_blanks(self):
        parsed = conf.parse_dotenv("# comment\n\n  \nA=1\n")
        self.assertEqual(parsed, {"A": "1"})

    def test_quotes_are_stripped(self):
        parsed = conf.parse_dotenv('A="quoted"\nB=\'single\'\n')
        self.assertEqual(parsed, {"A": "quoted", "B": "single"})

    def test_export_prefix(self):
        self.assertEqual(conf.parse_dotenv("export A=1"), {"A": "1"})

    def test_line_without_equals_is_ignored(self):
        self.assertEqual(conf.parse_dotenv("nonsense\nA=1"), {"A": "1"})

    def test_value_may_contain_equals(self):
        self.assertEqual(conf.parse_dotenv("A=a=b"), {"A": "a=b"})

    def test_empty_value(self):
        self.assertEqual(conf.parse_dotenv("A="), {"A": ""})


class EnvHelperTests(unittest.TestCase):
    def test_env_int_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(conf._env_int("MISSING", 7), 7)

    def test_env_int_override(self):
        with mock.patch.dict(os.environ, {"X": "42"}, clear=True):
            self.assertEqual(conf._env_int("X", 7), 42)

    def test_env_int_invalid(self):
        with mock.patch.dict(os.environ, {"X": "abc"}, clear=True), self.assertRaises(
            conf.ConfigurationError
        ):
            conf._env_int("X", 7)

    def test_env_bool(self):
        for raw, expected in (
            ("1", True),
            ("true", True),
            ("YES", True),
            ("0", False),
            ("off", False),
        ):
            with mock.patch.dict(os.environ, {"X": raw}, clear=True):
                self.assertEqual(conf._env_bool("X", not expected), expected)


class PathTests(unittest.TestCase):
    def test_share_dir_is_absolute(self):
        self.assertTrue(conf.SHARE_DIR.is_absolute())

    def test_project_root_contains_server_dir(self):
        self.assertTrue((conf.PROJECT_ROOT / "server" / "conf.py").is_file())


class DbSettingsTests(unittest.TestCase):
    def test_settings_shape(self):
        settings = conf.db_settings()
        for key in ("host", "port", "user", "database", "table"):
            self.assertIn(key, settings)
        self.assertIsInstance(settings["port"], int)

    def test_redacted_hides_password(self):
        with mock.patch.object(conf, "DB_PASSWORD", "topsecret"):
            self.assertEqual(conf.redacted()["db"]["password"], "***")
            self.assertNotIn("topsecret", str(conf.redacted()))


class ValidateTests(unittest.TestCase):
    def test_missing_password_is_reported(self):
        with mock.patch.object(conf, "DB_PASSWORD", ""):
            with self.assertRaises(conf.ConfigurationError) as ctx:
                conf.validate()
            self.assertIn("WEBDAV_DB_PASSWORD", str(ctx.exception))

    def test_valid_configuration_passes(self):
        with mock.patch.object(conf, "DB_PASSWORD", "x"), mock.patch.object(
            conf, "HOST", "127.0.0.1"
        ):
            conf.validate()


if __name__ == "__main__":
    unittest.main()
