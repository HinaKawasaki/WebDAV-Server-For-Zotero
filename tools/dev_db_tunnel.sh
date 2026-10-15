#!/usr/bin/env bash
#
# Forward the remote MariaDB port to the local machine, for development.
#
# The production server connects to MariaDB on 127.0.0.1:3306 and MariaDB is
# configured to listen on the loopback interface only, so a plain network
# connection from outside cannot reach it.  This opens an SSH local forward:
#
#     127.0.0.1:<local_port>  --ssh-->  <server>:127.0.0.1:3306
#
# Point WEBDAV_DB_HOST/WEBDAV_DB_PORT in .env at the forwarded port.
#
# Usage:
#     tools/dev_db_tunnel.sh [user@]host [local_port]
#
# The target may also be given via the WEBDAV_DEV_SSH environment variable.

set -euo pipefail

target="${1:-${WEBDAV_DEV_SSH:-}}"
local_port="${2:-13306}"

if [[ -z "${target}" ]]; then
    echo "usage: $0 [user@]host [local_port]" >&2
    echo "   or: WEBDAV_DEV_SSH=user@host $0" >&2
    exit 2
fi

echo "[*] forwarding 127.0.0.1:${local_port} -> ${target}:127.0.0.1:3306"
echo "[*] set WEBDAV_DB_HOST=127.0.0.1 and WEBDAV_DB_PORT=${local_port} in .env"
echo "[*] press Ctrl+C to close the tunnel"

exec ssh -N \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -L "127.0.0.1:${local_port}:127.0.0.1:3306" \
    "${target}"
