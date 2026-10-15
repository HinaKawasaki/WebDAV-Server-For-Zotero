#!/usr/bin/env bash
#
# Cron-friendly health check for a deployed WebDAV instance.
#
# Exit code: 0 healthy, 1 warning, 2 critical - so a cron job or systemd timer
# can alert on any non-zero result.
#
#   tools/ops_check.sh [--port 8000] [--warn 80] [--crit 90]
#                      [--path INFO]   probe path; defaults to /<user>/ when
#                                      --user is given, otherwise /
#                      [--data-dir DIR] [--url http://127.0.0.1:8000]
#                      [--user NAME --password PW]        # deep check: log in
#                      [--skip service|listener|http|db|disk]   (repeatable)
#                      [--quiet]                          # only print problems
#
# Checks
#   service   systemd unit webdav.service is active
#   listener  something is listening on the configured port
#   http      the DAV endpoint answers 401 without credentials (or 207 with them)
#   db        `manage listusers` succeeds (MariaDB reachable, .env correct)
#   disk      usage of the storage filesystem is below the thresholds
#
# Cron example (every 15 minutes, mail on problems):
#   */15 * * * *  /home/ubuntu/webdav-server/tools/ops_check.sh --quiet \
#                   --user hello --password "$WEBDAV_PASSWORD" || /usr/bin/mail -s "webdav" you

set -euo pipefail

source "$(dirname "$(readlink -f "$0")")/lib.sh"

ROOT="$(repo_root)"
PORT=""
URL=""
PATH_INFO=""
WARN=80
CRIT=90
DATA_DIR=""
PROBE_USER=""
PROBE_PASSWORD=""
TIMEOUT=5
QUIET=0
SKIP=()

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:?}"; shift 2 ;;
        --url) URL="${2:?}"; shift 2 ;;
        --path) PATH_INFO="${2:?}"; shift 2 ;;
        --warn) WARN="${2:?}"; shift 2 ;;
        --crit) CRIT="${2:?}"; shift 2 ;;
        --data-dir) DATA_DIR="${2:?}"; shift 2 ;;
        --user) PROBE_USER="${2:?}"; shift 2 ;;
        --password) PROBE_PASSWORD="${2:?}"; shift 2 ;;
        --timeout) TIMEOUT="${2:?}"; shift 2 ;;
        --skip) SKIP+=("${2:?}"); shift 2 ;;
        --quiet) QUIET=1; shift ;;
        -h | --help) sed -n '2,30p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

load_dotenv "$ROOT/.env"
PORT="${PORT:-$(env_or "${WEBDAV_PORT:-}" 8000)}"
# Probe the address the server actually binds: with WEBDAV_HOST=172.16.1.1
# (VPN direct) loopback is not served at all.  A wildcard is not connectable,
# so fall back to loopback for it.
bind_host="$(env_or "${WEBDAV_HOST:-}" 127.0.0.1)"
case "$bind_host" in 0.0.0.0 | :: | "") bind_host="127.0.0.1" ;; esac
URL="${URL:-http://$bind_host:$PORT}"
DATA_DIR="${DATA_DIR:-${WEBDAV_SHARE_DIR:-}}"
PATH_INFO="${PATH_INFO:-/${PROBE_USER:+$PROBE_USER/}}"
case "$PATH_INFO" in /*) ;; *) PATH_INFO="/$PATH_INFO" ;; esac

skip() {
    local want="$1" item
    for item in "${SKIP[@]:-}"; do
        [ "$item" = "$want" ] && return 0
    done
    return 1
}

WORST=0
report() {
    local level="$1" name="$2" detail="$3"
    case "$level" in
        CRIT) [ 2 -gt "$WORST" ] && WORST=2 || true ;;
        WARN) [ 1 -gt "$WORST" ] && WORST=1 || true ;;
    esac
    if [ "$QUIET" -eq 1 ] && [ "$level" = "OK" ]; then
        return 0
    fi
    printf '%-4s %-9s %s\n' "$level" "$name" "$detail"
}

# ------------------------------------------------------------------- service
if skip service; then
    :
elif ! command -v systemctl >/dev/null; then
    report WARN service "systemctl not available (not running under systemd?)"
else
    state="$(systemctl is-active webdav.service 2>/dev/null || true)"
    if [ "$state" = "active" ]; then
        report OK service "webdav.service is active"
    else
        report CRIT service "webdav.service is '$state' (expected active)"
    fi
fi

# ------------------------------------------------------------------ listener
if skip listener; then
    :
else
    if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
        report OK listener "listening on port $PORT"
    else
        report CRIT listener "nothing is listening on port $PORT"
    fi
fi

# ---------------------------------------------------------------------- http
if skip http; then
    :
elif ! command -v curl >/dev/null; then
    report WARN http "curl not available"
else
    args=(-s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT")
    if [ -n "$PROBE_USER" ]; then
        code="$(curl "${args[@]}" -u "$PROBE_USER:$PROBE_PASSWORD" -X PROPFIND \
            -H 'Depth: 0' "$URL$PATH_INFO" || true)"
        if [ "$code" = "207" ]; then
            report OK http "authenticated PROPFIND $PATH_INFO -> 207"
        else
            report CRIT http "authenticated PROPFIND $PATH_INFO -> $code (expected 207)"
        fi
    else
        code="$(curl "${args[@]}" -X PROPFIND -H 'Depth: 0' "$URL$PATH_INFO" || true)"
        if [ "$code" = "401" ]; then
            report OK http "anonymous PROPFIND $PATH_INFO -> 401"
        else
            report CRIT http "anonymous PROPFIND $PATH_INFO -> $code (expected 401)"
        fi
    fi
fi

# ------------------------------------------------------------------------ db
if skip db; then
    :
else
    python_bin="${WEBDAV_PYTHON:-$ROOT/.venv/bin/python}"
    if [ ! -x "$python_bin" ]; then
        report WARN db "no virtualenv at $python_bin (set WEBDAV_PYTHON)"
    elif (cd "$ROOT" && "$python_bin" -m server.manage listusers >/dev/null 2>&1); then
        report OK db "manage listusers succeeded"
    else
        report CRIT db "manage listusers failed (check journalctl -u webdav.service)"
    fi
fi

# ---------------------------------------------------------------------- disk
if skip disk; then
    :
elif [ -z "$DATA_DIR" ]; then
    report WARN disk "no storage dir configured (set --data-dir or WEBDAV_SHARE_DIR)"
elif [ ! -d "$DATA_DIR" ]; then
    report CRIT disk "storage dir does not exist: $DATA_DIR"
else
    used="$(df -P "$DATA_DIR" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    if [ -z "$used" ]; then
        report WARN disk "could not read disk usage for $DATA_DIR"
    elif [ "$used" -ge "$CRIT" ]; then
        report CRIT disk "$DATA_DIR is ${used}% full (crit $CRIT%, warn $WARN%)"
    elif [ "$used" -ge "$WARN" ]; then
        report WARN disk "$DATA_DIR is ${used}% full (warn $WARN%)"
    else
        report OK disk "$DATA_DIR is ${used}% full"
    fi
fi

exit "$WORST"
