# server/conf.py
"""Configuration for the Zotero WebDAV server.

Settings are resolved in this order (later wins):

1. the defaults below,
2. a ``.env`` file in the project root,
3. real environment variables.

No credential is ever stored in the source tree - see ``.env.example``.
Paths are resolved to absolute paths so the server behaves identically no
matter which directory it was started from.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_ROOT = Path(__file__).resolve().parent


class ConfigurationError(RuntimeError):
    """Raised when the configuration cannot be used to start the server."""


# --------------------------------------------------------------------------- #
# .env support (a deliberately tiny parser - no third-party dependency)
# --------------------------------------------------------------------------- #
def parse_dotenv(text: str) -> dict:
    """Parse ``KEY=VALUE`` lines from a .env file body."""
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_dotenv(path, override: bool = False) -> dict:
    """Load a .env file into ``os.environ`` and return the parsed values."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    values = parse_dotenv(text)
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


load_dotenv(PROJECT_ROOT / ".env")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _env(name: str, default=None):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return int(default)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
#: Bind address.  The service is designed to be reached through the SSH tunnel,
#: so the default is loopback only.  Exposing it on 0.0.0.0 sends Basic
#: credentials in clear text to everyone who can reach the host.
HOST = _env("WEBDAV_HOST", "127.0.0.1")
PORT = _env_int("WEBDAV_PORT", 8000)
VERBOSE = _env_int("WEBDAV_VERBOSE", 3)
NUM_THREADS = _env_int("WEBDAV_NUM_THREADS", 20)
REQUEST_QUEUE_SIZE = _env_int("WEBDAV_REQUEST_QUEUE_SIZE", 128)
SOCKET_TIMEOUT = _env_int("WEBDAV_SOCKET_TIMEOUT", 60)
SHUTDOWN_TIMEOUT = _env_int("WEBDAV_SHUTDOWN_TIMEOUT", 10)

LOG_LEVEL = (_env("WEBDAV_LOG_LEVEL", "INFO") or "INFO").upper()
LOG_FILE = _env("WEBDAV_LOG_FILE")

#: Hide the WsgiDAV/Python version in error pages and response headers.
SUPPRESS_VERSION_INFO = _env_bool("WEBDAV_SUPPRESS_VERSION_INFO", True)

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
SHARE_DIR = (
    Path(_env("WEBDAV_SHARE_DIR", str(PROJECT_ROOT / "zotero")))
    .expanduser()
    .resolve()
)

#: Give every account its own directory below SHARE_DIR, served at
#: ``/<username>/``.  Setting this to false restores the old shared root (and
#: the cross-user lock/property collisions that come with it).
PER_USER_STORAGE = _env_bool("WEBDAV_PER_USER_STORAGE", True)

# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
REALM = _env("WEBDAV_REALM", "Zotero WebDAV Storage")

#: Failed logins for one key (user + source address) are remembered for
#: ``LOGIN_WINDOW_SECONDS``; once there are ``LOGIN_MAX_ATTEMPTS`` of them the
#: key must wait a penalty that doubles per extra failure, up to
#: ``LOGIN_MAX_PENALTY`` seconds.  0 attempts disables the throttle.
LOGIN_MAX_ATTEMPTS = _env_int("WEBDAV_LOGIN_MAX_ATTEMPTS", 10)
LOGIN_WINDOW_SECONDS = _env_int("WEBDAV_LOGIN_WINDOW_SECONDS", 300)
LOGIN_BASE_PENALTY = _env_int("WEBDAV_LOGIN_BASE_PENALTY", 2)
LOGIN_MAX_PENALTY = _env_int("WEBDAV_LOGIN_MAX_PENALTY", 300)

#: A successful authentication is remembered for this long so that a burst of
#: requests from one client does not repeat the database round trip and the
#: scrypt verification every time.  0 disables the cache.
AUTH_CACHE_SECONDS = _env_int("WEBDAV_AUTH_CACHE_SECONDS", 45)
AUTH_CACHE_MAX_ENTRIES = _env_int("WEBDAV_AUTH_CACHE_MAX_ENTRIES", 512)
#: Touching this file invalidates every cached verification; ``manage.py`` does
#: that on adduser/passwd/deluser so a changed password applies immediately.
AUTH_CACHE_EPOCH_FILE = Path(
    _env("WEBDAV_AUTH_CACHE_EPOCH_FILE", str(SHARE_DIR / ".auth-cache.epoch"))
).expanduser()

# --------------------------------------------------------------------------- #
# Database (MariaDB / MySQL)
# --------------------------------------------------------------------------- #
DB_HOST = _env("WEBDAV_DB_HOST", "127.0.0.1")
DB_PORT = _env_int("WEBDAV_DB_PORT", 3306)
DB_USER = _env("WEBDAV_DB_USER", "webdav")
DB_PASSWORD = _env("WEBDAV_DB_PASSWORD", "")
DB_NAME = _env("WEBDAV_DB_NAME", "webdav")
DB_TABLE = _env("WEBDAV_DB_TABLE", "dav_users")
DB_CONNECT_TIMEOUT = _env_int("WEBDAV_DB_CONNECT_TIMEOUT", 5)
DB_READ_TIMEOUT = _env_int("WEBDAV_DB_READ_TIMEOUT", 15)
DB_WRITE_TIMEOUT = _env_int("WEBDAV_DB_WRITE_TIMEOUT", 15)


def db_settings() -> dict:
    """Return the database settings as a plain dict."""
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
        "table": DB_TABLE,
        "connect_timeout": DB_CONNECT_TIMEOUT,
        "read_timeout": DB_READ_TIMEOUT,
        "write_timeout": DB_WRITE_TIMEOUT,
    }


def build_config() -> dict:
    """Build the WsgiDAV application configuration."""
    return {
        "host": HOST,
        "port": PORT,
        "verbose": VERBOSE,
        "provider_mapping": {"/": str(SHARE_DIR)},
        "suppress_version_info": SUPPRESS_VERSION_INFO,
        # In-memory dead-property store: better than None for clients that issue
        # PROPPATCH, but note it does not survive a restart.
        "property_manager": True,
        # We configure logging ourselves in start_server.py.
        "logging": {"enable": False},
        "webdav_db": db_settings(),
        "webdav_auth": {
            "realm": REALM,
            "login_max_attempts": LOGIN_MAX_ATTEMPTS,
            "login_window_seconds": LOGIN_WINDOW_SECONDS,
            "login_base_penalty": LOGIN_BASE_PENALTY,
            "login_max_penalty": LOGIN_MAX_PENALTY,
        },
        "webdav_auth_cache": {
            "ttl_seconds": AUTH_CACHE_SECONDS,
            "max_entries": AUTH_CACHE_MAX_ENTRIES,
            "epoch_file": str(AUTH_CACHE_EPOCH_FILE),
        },
    }


#: Kept for convenience; prefer :func:`build_config` so environment changes are
#: picked up.
config = build_config()


def redacted() -> dict:
    """Return a loggable snapshot of the effective configuration."""
    return {
        "host": HOST,
        "port": PORT,
        "share_dir": str(SHARE_DIR),
        "per_user_storage": PER_USER_STORAGE,
        "realm": REALM,
        "auth": {
            "cache_seconds": AUTH_CACHE_SECONDS,
            "throttle_attempts": LOGIN_MAX_ATTEMPTS,
            "throttle_base_penalty": LOGIN_BASE_PENALTY,
            "throttle_max_penalty": LOGIN_MAX_PENALTY,
        },
        "db": {
            "host": DB_HOST,
            "port": DB_PORT,
            "user": DB_USER,
            "database": DB_NAME,
            "table": DB_TABLE,
            "password": "***" if DB_PASSWORD else "(unset)",
        },
    }


def validate() -> None:
    """Fail fast on configurations that cannot work."""
    problems = []
    if not DB_USER:
        problems.append("WEBDAV_DB_USER is empty")
    if not DB_PASSWORD:
        problems.append(
            "WEBDAV_DB_PASSWORD is empty - set it in .env or the environment"
        )
    if not DB_NAME:
        problems.append("WEBDAV_DB_NAME is empty")
    if PORT < 1 or PORT > 65535:
        problems.append(f"WEBDAV_PORT out of range: {PORT}")
    if problems:
        raise ConfigurationError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
        )
    if HOST not in {"127.0.0.1", "localhost", "::1"}:
        # Not fatal: the tunnel setup may legitimately need another address.
        import logging

        logging.getLogger(__name__).warning(
            "WEBDAV_HOST=%s is not a loopback address; Basic credentials would "
            "travel in clear text on the network unless TLS is terminated in "
            "front of this server.",
            HOST,
        )
