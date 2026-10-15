# server/passwords.py
"""Password hashing and verification.

New hashes are self-describing and deliberately compact so they fit the
historical ``VARCHAR(64)`` password column::

    scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>

with base64url (unpadded) salt/hash.  For the default work factors the value is
63 characters long - see :func:`hash_password` and :data:`MAX_HASH_LENGTH`.

The verifier also understands the formats that previous revisions of this
project wrote, so existing databases keep working and are upgraded to scrypt
automatically the next time a user logs in successfully:

* ``scrypt$...``      - salted scrypt (current format)
* ``sha256$<hex>``    - tagged, unsalted SHA-256
* ``<64 hex chars>``  - untagged, unsalted SHA-256 (revision 28c2ddf)
* anything else       - legacy plaintext password (pre-refactor server)

Secrets are never logged by this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import string

__all__ = [
    "MAX_HASH_LENGTH",
    "SCRYPT_PREFIX",
    "generate_password",
    "hash_password",
    "hash_length",
    "looks_hashed",
    "verify_password",
]

SCRYPT_PREFIX = "scrypt"
_SHA256_PREFIX = "sha256"

# scrypt work factors (memory cost 128 * n * r == 16 MiB for the defaults)
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
# Salt and output sizes are chosen so the encoded value - with the work
# factors below written out in full - stays well below MAX_HASH_LENGTH.
# 12 byte salt and a 128 bit tag are both far beyond what a password verifier
# needs; the scrypt work factor is what resists guessing.
_SALT_BYTES = 12
_KEY_BYTES = 16

#: Historical width of the ``dav_users.password`` column.  The encoding must
#: never exceed it, otherwise existing deployments would have to ALTER the
#: table before they can store a password.
MAX_HASH_LENGTH = 64

_HEX_DIGITS = frozenset(string.hexdigits)
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES
    )


def generate_password(length: int = 20) -> str:
    """Return a cryptographically strong random password."""
    if length < 8:
        raise ValueError("password length must be at least 8")
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def hash_password(password: str) -> str:
    """Hash ``password`` with a fresh random salt."""
    if not isinstance(password, str) or not password:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    encoded = "$".join(
        (
            SCRYPT_PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64_encode(salt),
            _b64_encode(digest),
        )
    )
    if len(encoded) > MAX_HASH_LENGTH:
        # Fail loudly at start-up (via check_schema/hash_length) instead of
        # hitting "Data too long for column 'password'" at INSERT time.
        raise ValueError(
            f"encoded password hash is {len(encoded)} characters, which exceeds "
            f"the {MAX_HASH_LENGTH}-character column limit"
        )
    return encoded


def hash_length() -> int:
    """Length of a hash produced with the current parameters.

    Constant for a given build, so it can be compared against the width of the
    password column.
    """
    return len(hash_password("length-probe"))


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(char in _HEX_DIGITS for char in value)


def _verify_scrypt(password: str, stored: str) -> tuple:
    parts = stored.split("$")
    if len(parts) != 6:
        return False, False
    _, n_raw, r_raw, p_raw, salt_raw, digest_raw = parts
    try:
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = _b64_decode(salt_raw)
        expected = _b64_decode(digest_raw)
    except (ValueError, binascii.Error):
        return False, False
    if n <= 1 or r < 1 or p < 1 or not salt or not expected:
        return False, False
    try:
        actual = _derive(password, salt, n, r, p)
    except ValueError:
        # Parameters this build of OpenSSL refuses to honour.
        return False, False
    ok = hmac.compare_digest(actual, expected)
    stale = (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
    return ok, bool(ok and stale)


def verify_password(password: str, stored: str) -> tuple:
    """Check ``password`` against a stored value.

    Returns a ``(ok, needs_rehash)`` tuple.  ``needs_rehash`` is true when the
    value verified fine but was stored with an outdated (or legacy) scheme, so
    the caller should write back :func:`hash_password` of the same password.
    """
    if not isinstance(password, str) or not isinstance(stored, str) or not stored:
        return False, False

    if stored.startswith(SCRYPT_PREFIX + "$"):
        return _verify_scrypt(password, stored)

    if stored.startswith(_SHA256_PREFIX + "$"):
        expected = stored[len(_SHA256_PREFIX) + 1 :]
        actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
        ok = hmac.compare_digest(actual, expected.lower())
        return ok, bool(ok)

    if _is_sha256_hex(stored):
        actual = hashlib.sha256(password.encode("utf-8")).hexdigest()
        ok = hmac.compare_digest(actual, stored.lower())
        return ok, bool(ok)

    # Legacy plaintext column.
    ok = hmac.compare_digest(password.encode("utf-8"), stored.encode("utf-8"))
    return ok, bool(ok)


def looks_hashed(stored: str) -> bool:
    """Return True if ``stored`` is in one of the recognised hash formats."""
    if not isinstance(stored, str):
        return False
    return (
        stored.startswith(SCRYPT_PREFIX + "$")
        or stored.startswith(_SHA256_PREFIX + "$")
        or _is_sha256_hex(stored)
    )
