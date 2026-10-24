# tests/test_passwords.py
import unittest

from server import passwords


class HashPasswordTests(unittest.TestCase):
    def test_roundtrip(self):
        stored = passwords.hash_password("correct horse battery staple")
        self.assertTrue(stored.startswith("scrypt$"))
        ok, needs_rehash = passwords.verify_password(
            "correct horse battery staple", stored
        )
        self.assertTrue(ok)
        self.assertFalse(needs_rehash)

    def test_wrong_password(self):
        stored = passwords.hash_password("s3cret")
        ok, _ = passwords.verify_password("s3cret ", stored)
        self.assertFalse(ok)

    def test_salt_is_random(self):
        self.assertNotEqual(
            passwords.hash_password("same"), passwords.hash_password("same")
        )

    def test_empty_password_rejected(self):
        with self.assertRaises(ValueError):
            passwords.hash_password("")

    def test_generated_password(self):
        password = passwords.generate_password(24)
        self.assertEqual(len(password), 24)
        self.assertNotEqual(password, passwords.generate_password(24))
        with self.assertRaises(ValueError):
            passwords.generate_password(4)


class ColumnWidthTests(unittest.TestCase):
    """The production column is VARCHAR(64); the encoding must fit it."""

    def test_hash_fits_the_legacy_varchar64_column(self):
        self.assertLessEqual(passwords.hash_length(), passwords.MAX_HASH_LENGTH)
        self.assertLessEqual(len(passwords.hash_password("x")), 64)

    def test_hash_length_is_stable(self):
        self.assertEqual(
            passwords.hash_length(), len(passwords.hash_password("another"))
        )


class LegacyFormatTests(unittest.TestCase):
    def test_plaintext_is_accepted_and_flagged(self):
        ok, needs_rehash = passwords.verify_password("admin", "admin")
        self.assertTrue(ok)
        self.assertTrue(needs_rehash)

    def test_plaintext_wrong(self):
        ok, _ = passwords.verify_password("nope", "admin")
        self.assertFalse(ok)

    def test_untagged_sha256_is_accepted_and_flagged(self):
        import hashlib

        stored = hashlib.sha256(b"admin").hexdigest()
        ok, needs_rehash = passwords.verify_password("admin", stored)
        self.assertTrue(ok)
        self.assertTrue(needs_rehash)

    def test_tagged_sha256_is_accepted_and_flagged(self):
        import hashlib

        stored = "sha256$" + hashlib.sha256(b"admin").hexdigest()
        ok, needs_rehash = passwords.verify_password("admin", stored)
        self.assertTrue(ok)
        self.assertTrue(needs_rehash)

    def test_sha256_with_different_case(self):
        import hashlib

        stored = hashlib.sha256(b"admin").hexdigest().upper()
        ok, _ = passwords.verify_password("admin", stored)
        self.assertTrue(ok)

    def test_malformed_values_do_not_raise(self):
        for stored in ("", None, "scrypt$bogus", "scrypt$1$2$3$%%%$%%%", "$"):
            ok, needs_rehash = passwords.verify_password("x", stored)
            self.assertFalse(ok)
            self.assertFalse(needs_rehash)

    def test_looks_hashed(self):
        self.assertTrue(passwords.looks_hashed(passwords.hash_password("x")))
        self.assertTrue(passwords.looks_hashed("a" * 64))
        self.assertFalse(passwords.looks_hashed("plaintext"))


class PasswordPolicyTests(unittest.TestCase):
    def test_a_long_enough_password_is_accepted(self):
        passwords.validate_password("correct-horse-battery")
        passwords.validate_password("pässwörd-✓-1234")

    def test_too_short_is_rejected(self):
        with self.assertRaises(ValueError):
            passwords.validate_password("a" * (passwords.MIN_PASSWORD_LENGTH - 1))

    def test_the_generated_password_satisfies_the_policy(self):
        passwords.validate_password(passwords.generate_password())

    def test_the_user_name_is_rejected(self):
        with self.assertRaises(ValueError):
            passwords.validate_password("alicealicealice", username="alicealicealice")

    def test_a_single_repeated_character_is_rejected(self):
        with self.assertRaises(ValueError):
            passwords.validate_password("x" * 16)

    def test_empty_and_non_string_are_rejected(self):
        for value in ("", None, 42):
            with self.assertRaises(ValueError):
                passwords.validate_password(value)


if __name__ == "__main__":
    unittest.main()
