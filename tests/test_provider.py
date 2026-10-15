# tests/test_provider.py
import tempfile
import unittest
from pathlib import Path

# Importing server.provider first pulls in wsgidav.util, which wsgidav.dav_error
# needs; importing wsgidav.dav_error directly trips its circular import guard.
from server.provider import PerUserFilesystemProvider
from wsgidav.dav_error import DAVError


def environ_for(username):
    return {"wsgidav.auth.user_name": username, "REQUEST_METHOD": "GET"}


class PerUserProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "storage"
        self.base.mkdir()
        self.provider = PerUserFilesystemProvider(str(self.base), fs_opts={})
        self.provider.set_share_path("/")

    def tearDown(self):
        self._tmp.cleanup()

    def test_path_maps_into_the_users_own_directory(self):
        result = self.provider._loc_to_file_path(
            "/alice/sub/file.txt", environ_for("alice")
        )
        self.assertEqual(Path(result), self.base / "alice" / "sub" / "file.txt")

    def test_user_root_is_created_on_demand(self):
        result = self.provider._loc_to_file_path("/alice", environ_for("alice"))
        self.assertTrue(Path(result).is_dir())

    def test_root_is_rejected(self):
        for path in ("/", "", "/."):
            with self.assertRaises(DAVError) as ctx:
                self.provider._loc_to_file_path(path, environ_for("alice"))
            self.assertEqual(ctx.exception.args[0], 403)

    def test_other_users_prefix_is_rejected(self):
        with self.assertRaises(DAVError) as ctx:
            self.provider._loc_to_file_path("/bob/secret.txt", environ_for("alice"))
        self.assertEqual(ctx.exception.args[0], 403)

    def test_prefix_collisions_are_rejected(self):
        # /alice2 must not be reachable through a prefix match on /alice
        with self.assertRaises(DAVError):
            self.provider._loc_to_file_path("/alice2/x", environ_for("alice"))

    def test_dotdot_escapes_are_rejected(self):
        for path in ("/alice/../bob/x", "/alice/a/../../bob/x", "/alice/.."):
            with self.assertRaises(DAVError) as ctx:
                self.provider._loc_to_file_path(path, environ_for("alice"))
            self.assertEqual(ctx.exception.args[0], 403)

    def test_unauthenticated_is_rejected(self):
        with self.assertRaises(DAVError):
            self.provider._loc_to_file_path("/alice/x", {})

    def test_get_resource_inst_isolates_users(self):
        (self.base / "alice").mkdir()
        (self.base / "bob").mkdir()
        (self.base / "alice" / "a.txt").write_text("alice")
        (self.base / "bob" / "b.txt").write_text("bob")

        env = environ_for("alice")
        env["wsgidav.provider"] = self.provider

        resource = self.provider.get_resource_inst("/alice/a.txt", env)
        self.assertIsNotNone(resource)
        with self.assertRaises(DAVError):
            self.provider.get_resource_inst("/bob/b.txt", env)

    def test_ref_urls_are_per_user(self):
        """Lock and dead-property keys are derived from the URL."""
        (self.base / "alice").mkdir()
        (self.base / "bob").mkdir()
        (self.base / "alice" / "same.txt").write_text("a")
        (self.base / "bob" / "same.txt").write_text("b")

        alice_env = environ_for("alice")
        alice_env["wsgidav.provider"] = self.provider
        bob_env = environ_for("bob")
        bob_env["wsgidav.provider"] = self.provider

        alice_url = self.provider.get_resource_inst("/alice/same.txt", alice_env).get_ref_url()
        bob_url = self.provider.get_resource_inst("/bob/same.txt", bob_env).get_ref_url()

        self.assertNotEqual(alice_url, bob_url)
        self.assertIn("alice", alice_url)
        self.assertIn("bob", bob_url)

    def test_symlinks_are_refused(self):
        user_dir = self.base / "alice"
        user_dir.mkdir()
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (user_dir / "link").symlink_to(outside / "secret.txt")

        env = environ_for("alice")
        env["wsgidav.provider"] = self.provider
        with self.assertRaises(DAVError):
            self.provider.get_resource_inst("/alice/link", env)


if __name__ == "__main__":
    unittest.main()
