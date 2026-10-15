#!/usr/bin/env python3
# client/sshtunnel_client.py
"""Expose the remote WebDAV server on the local loopback interface.

The tunnel is kept alive: transient SSH failures are retried with exponential
backoff instead of terminating the process.  Press Ctrl+C (or send SIGTERM) to
stop it.
"""

from __future__ import annotations

import contextlib
import getpass
import logging
import signal
import sys
import threading
from pathlib import Path

try:
    from sshtunnel import SSHTunnelForwarder
except ImportError:  # pragma: no cover - only when sshtunnel is not installed
    SSHTunnelForwarder = None

logger = logging.getLogger("sshtunnel_client")

BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 2.0

#: how long to wait between liveness checks while the tunnel is up
LIVENESS_INTERVAL = 2.0


def backoff_delay(
    attempt: int,
    initial: float = BACKOFF_INITIAL,
    maximum: float = BACKOFF_MAX,
    factor: float = BACKOFF_FACTOR,
) -> float:
    """Delay before retry number ``attempt`` (0-based), capped at ``maximum``."""
    attempt = max(0, int(attempt))
    return min(maximum, initial * (factor**attempt))


def load_settings():
    """Import the local configuration, falling back to the example file."""
    try:
        import client_conf as settings

        return settings
    except ImportError:
        pass
    try:
        import client_conf_example as settings

        return settings
    except ImportError as exc:
        raise RuntimeError(
            "no client configuration found - copy client/client_conf_example.py "
            "to client/client_conf.py"
        ) from exc


def _resolve_password(settings) -> str:
    password = getattr(settings, "SSH_PASSWORD", "") or ""
    if password:
        return password
    if getattr(settings, "SSH_PKEY", ""):
        return ""
    # No key and no password configured: ask instead of storing a file secret.
    return getpass.getpass(
        f"SSH password for {getattr(settings, 'SSH_USER', '?')}@"
        f"{getattr(settings, 'SSH_HOST', '?')}: "
    )


def build_tunnel(settings):
    """Create an (unstarted) tunnel forwarder from a configuration module."""
    if SSHTunnelForwarder is None:
        raise RuntimeError(
            "the 'sshtunnel' package is missing - install it with "
            "'pip install -r client/requirements.txt'"
        )

    kwargs = {
        "ssh_username": settings.SSH_USER,
        "remote_bind_address": ("127.0.0.1", int(settings.REMOTE_PORT)),
        "local_bind_address": ("127.0.0.1", int(settings.LOCAL_PORT)),
        # Send keepalive packets so idle connections are not dropped.
        "set_keepalive": 30.0,
    }
    pkey = str(getattr(settings, "SSH_PKEY", "") or "")
    if pkey:
        kwargs["ssh_pkey"] = str(Path(pkey).expanduser())
        passphrase = getattr(settings, "SSH_PKEY_PASSPHRASE", "") or ""
        if passphrase:
            kwargs["ssh_private_key_password"] = passphrase
    else:
        kwargs["ssh_password"] = _resolve_password(settings)

    return SSHTunnelForwarder((settings.SSH_HOST, int(settings.SSH_PORT)), **kwargs)


def _print_banner(settings) -> None:
    webdav_user = getattr(settings, "WEBDAV_USER", "") or ""
    path = f"{webdav_user}/" if webdav_user else "<username>/"
    print("[*] tunnel established")
    print("[*] configure Zotero (Edit / Settings / Sync) with:")
    print(f"    - URL:  http://127.0.0.1:{settings.LOCAL_PORT}/{path}")
    print("    - user / password: a WebDAV account created with")
    print("      python -m server.manage adduser <name>")
    print(
        f"\n[!] forwarding 127.0.0.1:{settings.LOCAL_PORT} -> "
        f"{settings.SSH_HOST}:{settings.REMOTE_PORT} (as {settings.SSH_USER})"
    )


def run(settings=None, stop_event=None, max_attempts=None) -> int:
    """Keep the tunnel up until ``stop_event`` is set.

    ``max_attempts`` bounds the number of *consecutive* failed start attempts
    (mainly used by tests); ``None`` means retry forever.
    """
    if settings is None:
        settings = load_settings()
    stop = stop_event or threading.Event()
    attempt = 0

    while not stop.is_set():
        tunnel = None
        try:
            tunnel = build_tunnel(settings)
            tunnel.start()
        except Exception as exc:  # every failure means "retry"
            if tunnel is not None:
                with contextlib.suppress(Exception):
                    tunnel.stop()
            attempt += 1
            if max_attempts is not None and attempt >= max_attempts:
                logger.error("giving up after %s attempt(s): %s", attempt, exc)
                return 1
            delay = backoff_delay(attempt - 1)
            logger.warning("tunnel start failed (%s); retrying in %.0fs", exc, delay)
            stop.wait(delay)
            continue

        attempt = 0
        _print_banner(settings)
        try:
            while not stop.is_set() and tunnel.is_active:
                stop.wait(LIVENESS_INTERVAL)
            if not stop.is_set():
                logger.warning("tunnel dropped - reconnecting")
        finally:
            with contextlib.suppress(Exception):
                tunnel.stop()

    return 0


def main(argv=None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s - stopping", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    try:
        return run(stop_event=stop)
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
