# server/app.py
"""WsgiDAV application factory."""

from __future__ import annotations

import copy
import logging
import os

from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from server import conf
from server.auth import MariaDBDomainController
from server.provider import PerUserFilesystemProvider

logger = logging.getLogger(__name__)

__all__ = ["create_dav_app"]


def _build_provider(source: dict):
    """Create the DAV provider for the configured storage layout."""
    base = str(conf.SHARE_DIR)
    os.makedirs(base, exist_ok=True)

    fs_opts = dict(source.get("fs_dav_provider") or {})
    # Symlinks inside the served tree would be a way around the per-user
    # confinement, so they stay disabled.
    fs_opts.setdefault("follow_symlinks", False)

    if conf.PER_USER_STORAGE:
        logger.info("storage layout: one directory per user below %s", base)
        return PerUserFilesystemProvider(base, fs_opts=fs_opts)

    logger.warning(
        "WEBDAV_PER_USER_STORAGE is disabled: every account shares %s", base
    )
    return FilesystemProvider(base, fs_opts=fs_opts)


def create_dav_app(main_config=None) -> WsgiDAVApp:
    """Build the WsgiDAV application.

    ``webdav_db`` and ``webdav_auth`` travel inside the configuration dict and
    are picked up by :class:`server.auth.MariaDBDomainController`, which keeps
    all mutable state on the instance.
    """
    source = conf.build_config() if main_config is None else copy.deepcopy(main_config)

    source["provider_mapping"] = {"/": _build_provider(source)}
    source["http_authenticator"] = {
        "domain_controller": MariaDBDomainController,
        "accept_basic": True,
        # This controller has no way to look up the plaintext password needed
        # for digest authentication, so only Basic is offered.
        "accept_digest": False,
        "default_to_digest": False,
    }
    source["dir_browser"] = {"enable": False}

    return WsgiDAVApp(source)
