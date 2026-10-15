#!/usr/bin/env bash
#
# Back up a deployed WebDAV instance: the dav_users table and the storage tree.
#
# Usage (on the server, as the account that runs the service):
#
#   tools/backup.sh --dest /var/backups/webdav [--keep 14] [--data-mode archive]
#
#   --dest DIR         where the backups are written (required)
#   --keep N           how many of each kind to retain (default 14)
#   --data-mode MODE   archive | rsync | skip   (default archive)
#   --data DIR         storage tree    (default WEBDAV_SHARE_DIR from .env)
#   --env FILE         .env to read    (default <repo>/.env)
#   --skip-db          do not dump the table
#
# Cron example (daily at 03:17):
#   17 3 * * *  /home/ubuntu/webdav-server/tools/backup.sh --dest /var/backups/webdav --keep 14
#
# Restore:
#   gunzip -c webdav-db-<stamp>.sql.gz | mysql <database>
#   tar -xzf webdav-data-<stamp>.tar.gz -C <parent of the storage dir>

set -euo pipefail

source "$(dirname "$(readlink -f "$0")")/lib.sh"

DEST=""
KEEP=14
DATA_MODE="archive"
DATA_DIR=""
ENV_FILE=""
SKIP_DB=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dest) DEST="${2:?--dest needs a value}"; shift 2 ;;
        --keep) KEEP="${2:?--keep needs a value}"; shift 2 ;;
        --data-mode) DATA_MODE="${2:?--data-mode needs a value}"; shift 2 ;;
        --data) DATA_DIR="${2:?--data needs a value}"; shift 2 ;;
        --env) ENV_FILE="${2:?--env needs a value}"; shift 2 ;;
        --skip-db) SKIP_DB=1; shift ;;
        -h | --help) sed -n '2,25p' "$0"; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

[ -n "$DEST" ] || die "--dest is required"
case "$DATA_MODE" in archive | rsync | skip) ;; *) die "--data-mode must be archive, rsync or skip" ;; esac
case "$KEEP" in '' | *[!0-9]*) die "--keep must be a number" ;; esac

ROOT="$(repo_root)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
load_dotenv "$ENV_FILE"

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"
info "destination : $DEST"
info "stamp       : $STAMP"

# ------------------------------------------------------------------ database
if [ "$SKIP_DB" -eq 0 ]; then
    db_host="$(env_or "${WEBDAV_DB_HOST:-}" 127.0.0.1)"
    db_port="$(env_or "${WEBDAV_DB_PORT:-}" 3306)"
    db_user="${WEBDAV_DB_USER:-}"
    db_pass="${WEBDAV_DB_PASSWORD:-}"
    db_name="${WEBDAV_DB_NAME:-}"
    db_table="${WEBDAV_DB_TABLE:-dav_users}"

    [ -n "$db_user" ] || die "WEBDAV_DB_USER is not set (looked at $ENV_FILE)"
    [ -n "$db_name" ] || die "WEBDAV_DB_NAME is not set (looked at $ENV_FILE)"
    command -v mysqldump >/dev/null || die "mysqldump not found"

    out="$DEST/webdav-db-$STAMP.sql.gz"
    # Keep the password out of the process list.
    umask 077
    cnf="$(mktemp)"
    trap 'rm -f "$cnf"' EXIT
    {
        printf '[client]\n'
        printf 'host=%s\n' "$db_host"
        printf 'port=%s\n' "$db_port"
        printf 'user=%s\n' "$db_user"
        printf 'password=%s\n' "$db_pass"
    } >"$cnf"

    info "dumping     : $db_name.$db_table"
    mysqldump --defaults-extra-file="$cnf" --single-transaction --no-tablespaces \
        "$db_name" "$db_table" | gzip >"$out"
    rm -f "$cnf"
    trap - EXIT

    [ -s "$out" ] || die "database dump is empty: $out"
    gzip -t "$out" || die "database dump is corrupt: $out"
    info "wrote       : $out ($(stat -c %s "$out") bytes)"

    # retention (newest first)
    mapfile -t old < <(ls -1t "$DEST"/webdav-db-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)))
    for f in "${old[@]:-}"; do
        [ -n "$f" ] && rm -f "$f" && info "pruned      : $f"
    done
fi

# ---------------------------------------------------------------------- data
case "$DATA_MODE" in
    skip)
        info "data        : skipped"
        ;;
    archive)
        DATA_DIR="${DATA_DIR:-${WEBDAV_SHARE_DIR:-}}"
        [ -n "$DATA_DIR" ] || die "no storage dir (set --data or WEBDAV_SHARE_DIR)"
        [ -d "$DATA_DIR" ] || die "storage dir does not exist: $DATA_DIR"
        out="$DEST/webdav-data-$STAMP.tar.gz"
        parent="$(dirname "$DATA_DIR")"
        base="$(basename "$DATA_DIR")"
        info "archiving   : $DATA_DIR"
        tar -czf "$out" -C "$parent" "$base"
        gzip -t "$out" || die "data archive is corrupt: $out"
        info "wrote       : $out ($(stat -c %s "$out") bytes)"
        mapfile -t old < <(ls -1t "$DEST"/webdav-data-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)))
        for f in "${old[@]:-}"; do
            [ -n "$f" ] && rm -f "$f" && info "pruned      : $f"
        done
        ;;
    rsync)
        DATA_DIR="${DATA_DIR:-${WEBDAV_SHARE_DIR:-}}"
        [ -n "$DATA_DIR" ] || die "no storage dir (set --data or WEBDAV_SHARE_DIR)"
        [ -d "$DATA_DIR" ] || die "storage dir does not exist: $DATA_DIR"
        command -v rsync >/dev/null || die "rsync not found"
        target="$DEST/data-current"
        mkdir -p "$target"
        info "rsyncing    : $DATA_DIR -> $target"
        rsync -a --delete --stats "$DATA_DIR"/ "$target"/ >/dev/null
        info "wrote       : $target (mirror; keeps no history - use --data-mode archive for that)"
        ;;
esac

info "backup finished"
