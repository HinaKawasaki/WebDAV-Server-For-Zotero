# client/client_conf_example.py
"""Example client configuration.

Copy this file to ``client_conf.py`` and adjust the values, or leave them unset
and provide the environment variables instead.  ``client_conf.py`` is ignored by
git so real credentials are never committed.

Prefer key based authentication: point ``SSH_PKEY`` at a private key and leave
``SSH_PASSWORD`` empty.  If no password is given and no key is configured, the
client asks for the SSH password interactively.
"""

import os

# --- SSH connection ------------------------------------------------------- #
SSH_HOST = os.environ.get("ZOTERO_SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("ZOTERO_SSH_PORT", "22"))
SSH_USER = os.environ.get("ZOTERO_SSH_USER", "user")

# Key authentication (recommended).  Example: "~/.ssh/id_ed25519"
SSH_PKEY = os.environ.get("ZOTERO_SSH_PKEY", "")
SSH_PKEY_PASSPHRASE = os.environ.get("ZOTERO_SSH_PKEY_PASSPHRASE", "")

# Password authentication.  Leave empty to be prompted at start-up.
SSH_PASSWORD = os.environ.get("ZOTERO_SSH_PASSWORD", "")

# --- Port forwarding ------------------------------------------------------ #
# Port the WebDAV server listens on, on the remote side.
REMOTE_PORT = int(os.environ.get("ZOTERO_REMOTE_PORT", "8000"))
# Local port Zotero should be pointed at.
LOCAL_PORT = int(os.environ.get("ZOTERO_LOCAL_PORT", "8000"))

# --- WebDAV account ------------------------------------------------------- #
# Each account is served below /<username>/ on the server, so this value is
# used to print the exact URL to configure in Zotero.
WEBDAV_USER = os.environ.get("ZOTERO_WEBDAV_USER", "")
